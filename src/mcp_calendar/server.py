#!/usr/bin/env python
import os
import sys
import json
import time
import signal
import logging
import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Optional, Iterable
from datetime import datetime, date, timedelta, timezone
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

# =========================
# 환경 변수 및 로깅 설정
# =========================
load_dotenv()

level_name = os.getenv("MCP_LOG_LEVEL", "INFO").upper()
_level = getattr(logging, level_name, logging.INFO)
logging.basicConfig(
  level=_level,
  format="%(levelname)s: %(asctime)s - %(message)s",
  stream=sys.stderr
)
logger = logging.getLogger("gcal-mcp")

# =========================
# 설정/상수
# =========================
DEFAULT_TZ = os.getenv("MCP_TIMEZONE", "Asia/Seoul")

@dataclass(frozen=True)
class GAuthEnv:
  client_id: str
  client_secret: str
  refresh_token: str

  @staticmethod
  def from_env() -> "GAuthEnv":
    cid = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    csec = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    rtok = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()
    missing = [k for k, v in {
      "GOOGLE_CLIENT_ID": cid,
      "GOOGLE_CLIENT_SECRET": csec,
      "GOOGLE_REFRESH_TOKEN": rtok
    }.items() if not v]
    if missing:
      logger.error(f"Missing required env vars: {', '.join(missing)}")
      sys.exit(1)
    return GAuthEnv(cid, csec, rtok)

AUTH = GAuthEnv.from_env()

# =========================
# Google Calendar 서비스 캐시
# =========================
_service_lock = threading.Lock()
_service_cache: Optional[Any] = None

def get_calendar_service():
  global _service_cache
  if _service_cache is not None:
    return _service_cache
  with _service_lock:
    if _service_cache is not None:
      return _service_cache
    logger.debug("Creating OAuth2 credentials")
    creds = Credentials(
      None,
      refresh_token=AUTH.refresh_token,
      token_uri="https://oauth2.googleapis.com/token",
      client_id=AUTH.client_id,
      client_secret=AUTH.client_secret
    )
    logger.debug("Building Google Calendar v3 service")
    _service_cache = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service_cache

# =========================
# 유틸리티 함수들
# =========================
def _is_all_day_str(s: str) -> bool:
  s = (s or "").strip()
  return len(s) == 10 and s.count("-") == 2 and "T" not in s

def _parse_when(s: str, tz: str) -> dict:
  """
  ISO 8601 문자열을 Google Calendar 이벤트용 dict로 변환.
  - 'YYYY-MM-DD' 형식이면 종일 이벤트(date)
  - 나머지는 dateTime
  """
  s = (s or "").strip()
  if _is_all_day_str(s):
    # 종일 이벤트
    return {"date": s, "timeZone": tz}
  try:
    # 허용: 'Z' → '+00:00'
    iso = s.replace("Z", "+00:00")
    # fromisoformat 검증
    _ = datetime.fromisoformat(iso)
    return {"dateTime": s, "timeZone": tz}
  except Exception:
    raise ValueError(f"Invalid ISO time format: {s}")

def _normalize_attendees(attendees: Optional[Iterable[str]]) -> Optional[list[dict]]:
  if not attendees:
    return None
  out: list[dict] = []
  for e in attendees:
    if not e:
      continue
    email = e.strip()
    if email:
      out.append({"email": email})
  return out or None

def _normalize_reminders(reminders: Optional[dict]) -> dict:
  """
  Google Calendar reminders 필드 sanitize.
  """
  if not reminders:
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]}
  use_default = bool(reminders.get("useDefault", False))
  overrides = reminders.get("overrides") or []
  cleaned = []
  for r in overrides:
    method = (r.get("method") or "popup").strip()
    minutes = int(r.get("minutes", 10))
    minutes = max(0, min(minutes, 40320))  # 4주 제한 정도로 보호
    cleaned.append({"method": method, "minutes": minutes})
  return {"useDefault": use_default, "overrides": cleaned}

def _http_status_from_error(err: HttpError) -> Optional[int]:
  # googleapiclient의 HttpError는 resp.status 또는 status_code에 상태가 있음
  try:
    if hasattr(err, "status_code") and err.status_code:
      return int(err.status_code)
    if getattr(err, "resp", None) is not None and getattr(err.resp, "status", None):
      return int(err.resp.status)
  except Exception:
    return None
  return None

def _should_retry(exc: Exception) -> bool:
  if isinstance(exc, HttpError):
    status = _http_status_from_error(exc)
    if status is None:
      return False
    return status == 429 or 500 <= status < 600
  # 네트워크/임시 오류 등 일반 예외도 1-2회 재시도 가치
  return True

def _with_retries(func, *, retries: int = 3, base_delay: float = 0.6):
  """
  지수 백오프 + 지터(jitter) 리트라이 래퍼.
  """
  def _inner(*args, **kwargs):
    attempt = 0
    while True:
      try:
        return func(*args, **kwargs)
      except Exception as e:
        if attempt >= retries or not _should_retry(e):
          raise
        delay = base_delay * (2 ** attempt)
        # 간단 지터: 80% ~ 120%
        jitter = 0.8 + (0.4 * (time.time() % 1))
        sleep_for = delay * jitter
        logger.debug(f"Retry {attempt+1}/{retries} after error: {type(e).__name__} {e} (sleep {sleep_for:.2f}s)")
        time.sleep(sleep_for)
        attempt += 1
  return _inner

def _is_all_day_dict(when: dict) -> bool:
  return "date" in when and "dateTime" not in when

def _date_from_str(s: str) -> date:
  return datetime.strptime(s, "%Y-%m-%d").date()

def _ensure_end_after_start(start_when: dict, end_when: dict) -> tuple[dict, dict]:
  """
  Google Calendar는 종일 이벤트의 end.date가 exclusive.
  - start/end 모두 종일:
    - end가 start와 같거나 비었으면 end = start + 1day
    - end < start면 ValueError
  - dateTime 혼합/쌍:
    - fromisoformat으로 비교 후 end <= start면 ValueError
  """
  if _is_all_day_dict(start_when) and _is_all_day_dict(end_when):
    s = _date_from_str(start_when["date"])
    e = _date_from_str(end_when["date"])
    if e <= s:
      # 최소 하루 길이로 보정
      end_when = {**end_when, "date": (s + timedelta(days=1)).strftime("%Y-%m-%d")}
    return start_when, end_when

  # dateTime 비교
  def _to_dt(when: dict) -> datetime:
    if "dateTime" not in when:
      # 종일과 dateTime 혼합의 경우 자정으로 가정(현지 tz 문자열만 유지)
      return datetime.fromisoformat(when["date"] + "T00:00:00+09:00")
    iso = when["dateTime"].replace("Z", "+00:00")
    return datetime.fromisoformat(iso)

  sd = _to_dt(start_when)
  ed = _to_dt(end_when)
  if ed <= sd:
    raise ValueError("end_time must be after start_time")
  return start_when, end_when

# =========================
# MCP 서버 인스턴스
# =========================
mcp = FastMCP(
  "Google Calendar MCP",
  dependencies=[
    "python-dotenv",
    "google-api-python-client",
    "google-auth",
    "google-auth-oauthlib",
  ],
)

# =========================
# MCP Tools
# =========================
@mcp.tool()
async def health() -> str:
  """Simple health check"""
  return "ok"

@mcp.tool()
async def create_event(
  summary: str,
  start_time: str,
  end_time: str,
  description: str | None = None,
  location: str | None = None,
  attendees: list[str] | None = None,
  reminders: dict[str, Any] | None = None,
  calendar_id: str = "primary",
  timezone_str: str = DEFAULT_TZ,
  create_meet_link: bool = False
) -> str:
  """Create a calendar event with specified details

  Args:
    summary: Event title
    start_time: Start time (ISO format)
    end_time: End time (ISO format)
    description: Event description
    location: Event location
    attendees: List of attendee emails
    reminders: Reminder settings for the event
    calendar_id: Calendar ID (default: 'primary')
    timezone_str: Timezone ID (default: Asia/Seoul)
    create_meet_link: If True, enable Google Meet link

  Returns:
    String with event creation confirmation and link
  """
  tz = (timezone_str or DEFAULT_TZ).strip() or DEFAULT_TZ

  # 입력 정규화
  start_when = _parse_when(start_time, tz)
  end_when = _parse_when(end_time, tz)
  start_when, end_when = _ensure_end_after_start(start_when, end_when)

  attendees_norm = _normalize_attendees(attendees)
  reminders_norm = _normalize_reminders(reminders)

  logger.debug("create_event args: " + json.dumps({
    "summary": summary,
    "start": start_when,
    "end": end_when,
    "description": description,
    "location": location,
    "attendees": attendees_norm,
    "reminders": reminders_norm,
    "calendar_id": calendar_id,
    "timezone": tz,
    "create_meet_link": create_meet_link,
  }, ensure_ascii=False))

  event: dict[str, Any] = {
    "summary": summary,
    "start": start_when,
    "end": end_when,
    "reminders": reminders_norm
  }
  if description:
    event["description"] = description
  if location:
    event["location"] = location
  if attendees_norm:
    event["attendees"] = attendees_norm
  if create_meet_link:
    # 회의 링크 자동 생성
    event["conferenceData"] = {
      "createRequest": {"requestId": f"mcp-{int(time.time()*1000)}"}
    }

  def _insert():
    svc = get_calendar_service()
    req = svc.events().insert(calendarId=calendar_id, body=event, sendUpdates='none')
    if create_meet_link:
      req = req.conferenceDataVersion(1)
    return req.execute()

  try:
    # Google API 동기 호출 → 스레드에서 실행
    response = await asyncio.to_thread(_with_retries(_insert, retries=3, base_delay=0.6))
    logger.debug("Event insert response: " + json.dumps(response, ensure_ascii=False))
    link = response.get("htmlLink", "No link available")
    return f"Event created: {link}"
  except HttpError as http_err:
    # 상세 에러 메시지
    content = ""
    try:
      if getattr(http_err, "content", None):
        content = http_err.content.decode("utf-8", errors="ignore")
    except Exception:
      pass
    status = _http_status_from_error(http_err)
    logger.debug(f"HttpError status={status} content={content or str(http_err)}")
    raise Exception(f"Failed to create event: Google API error. {content or str(http_err)}") from http_err
  except Exception as error:
    import traceback
    logger.debug("General error: " + traceback.format_exc())
    raise Exception(f"Failed to create event: {type(error).__name__}: {str(error)}") from error

# =========================
# 종료/시그널 처리
# =========================
def _graceful_exit(signum, frame):
  logger.info(f"Received signal {signum}, shutting down...")
  sys.exit(0)

signal.signal(signal.SIGINT, _graceful_exit)
signal.signal(signal.SIGTERM, _graceful_exit)

# =========================
# 엔트리 포인트
# =========================
def main():
  """Run the MCP calendar server."""
  try:
    mcp.run()
  except KeyboardInterrupt:
    logger.info("Server stopped by user")
  except Exception as e:
    logger.error(f"Fatal error running server: {e}")
    sys.exit(1)

if __name__ == "__main__":
  main()