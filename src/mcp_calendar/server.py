#!/usr/bin/env python
import os
import sys
import json
import time
import signal
import logging
from typing import Any, Optional, Iterable
from datetime import datetime
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

# =========================
# 환경 변수 및 로깅 설정
# =========================
load_dotenv()

level_name = os.getenv("MCP_LOG_LEVEL", "DEBUG").upper()
_level = getattr(logging, level_name, logging.DEBUG)
logging.basicConfig(
  level=_level,
  format='DEBUG: %(asctime)s - %(message)s',
  stream=sys.stderr  # stderr 전용 로그
)
logger = logging.getLogger(__name__)

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
# 환경 변수 체크
# =========================
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REFRESH_TOKEN:
  logger.error("Error: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN environment variables are required")
  sys.exit(1)

# =========================
# 유틸리티 함수들
# =========================
def _parse_when(s: str) -> dict:
  """
  ISO 8601 문자열을 Google Calendar 이벤트용 dict로 변환.
  - 'YYYY-MM-DD' 형식이면 종일 이벤트(date)
  - 그 외엔 datetime으로 간주
  """
  s = (s or "").strip()
  if len(s) == 10 and s.count('-') == 2 and 'T' not in s:
    # 종일 이벤트
    return {'date': s, 'timeZone': 'Asia/Seoul'}
  try:
    iso = s.replace('Z', '+00:00')
    _ = datetime.fromisoformat(iso)  # 형식 검증
    return {'dateTime': s, 'timeZone': 'Asia/Seoul'}
  except Exception:
    raise ValueError(f"Invalid ISO time format: {s}")

def _normalize_attendees(attendees: Optional[Iterable[str]]) -> Optional[list[dict]]:
  if not attendees:
    return None
  out: list[dict] = []
  for e in attendees:
    e = (e or "").strip()
    if e:
      out.append({'email': e})
  return out or None

def _normalize_reminders(reminders: Optional[dict]) -> dict:
  if not reminders:
    return {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 10}]}
  use_default = bool(reminders.get('useDefault', False))
  overrides = reminders.get('overrides') or []
  cleaned = []
  for r in overrides:
    method = (r.get('method') or 'popup').strip()
    minutes = int(r.get('minutes', 10))
    cleaned.append({'method': method, 'minutes': minutes})
  return {'useDefault': use_default, 'overrides': cleaned}

def _with_retries(func, *, retries: int = 3, base_delay: float = 0.6):
  """
  간단한 지수 백오프 재시도 래퍼.
  429/5xx는 재시도, 그 외 예외는 최대 retries까지 시도.
  """
  def _inner(*args, **kwargs):
    attempt = 0
    while True:
      try:
        return func(*args, **kwargs)
      except HttpError as he:
        # 상태코드 추출
        status = None
        if hasattr(he, "status_code") and he.status_code:
          status = he.status_code
        elif getattr(he, "resp", None) is not None and getattr(he.resp, "status", None):
          status = int(he.resp.status)
        if status and (status == 429 or 500 <= int(status) < 600) and attempt < retries:
          delay = base_delay * (2 ** attempt)
          logger.debug(f"Retrying after HttpError {status} in {delay:.2f}s")
          time.sleep(delay)
          attempt += 1
          continue
        raise
      except Exception as e:
        if attempt < retries:
          delay = base_delay * (2 ** attempt)
          logger.debug(f"Retrying after error in {delay:.2f}s: {type(e).__name__} {e}")
          time.sleep(delay)
          attempt += 1
          continue
        raise
  return _inner

# =========================
# MCP Tool 구현
# =========================
@mcp.tool()
async def create_event(
  summary: str,
  start_time: str,
  end_time: str,
  description: str | None = None,
  location: str | None = None,
  attendees: list[str] | None = None,
  reminders: dict[str, Any] | None = None
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

  Returns:
    String with event creation confirmation and link
  """
  # 입력 정규화
  start_when = _parse_when(start_time)
  end_when = _parse_when(end_time)
  attendees_norm = _normalize_attendees(attendees)
  reminders_norm = _normalize_reminders(reminders)

  logger.debug('Creating calendar event with args: ' + json.dumps({
    'summary': summary,
    'start': start_when,
    'end': end_when,
    'description': description,
    'location': location,
    'attendees': attendees_norm,
    'reminders': reminders_norm
  }, ensure_ascii=False))

  try:
    logger.debug('Creating OAuth2 client')
    creds = Credentials(
      None,
      refresh_token=GOOGLE_REFRESH_TOKEN,
      token_uri="https://oauth2.googleapis.com/token",
      client_id=GOOGLE_CLIENT_ID,
      client_secret=GOOGLE_CLIENT_SECRET
    )
    logger.debug('OAuth2 client created')

    logger.debug('Creating calendar service')
    calendar_service = build('calendar', 'v3', credentials=creds)
    logger.debug('Calendar service created')

    event: dict[str, Any] = {
      'summary': summary,
      'start': start_when,
      'end': end_when,
      'reminders': reminders_norm
    }
    if description:
      event['description'] = description
    if location:
      event['location'] = location
      logger.debug(f'Location added: {location}')
    if attendees_norm:
      event['attendees'] = attendees_norm
      logger.debug('Attendees added: ' + json.dumps(attendees_norm, ensure_ascii=False))

    logger.debug('Attempting to insert event')

    def _insert():
      return calendar_service.events().insert(calendarId='primary', body=event).execute()

    response = _with_retries(_insert, retries=3, base_delay=0.6)()
    logger.debug('Event insert response: ' + json.dumps(response, ensure_ascii=False))

    return f"Event created: {response.get('htmlLink', 'No link available')}"

  except HttpError as http_err:
    # 구글 API 에러 상세 로깅
    content = ''
    try:
      if getattr(http_err, 'content', None):
        content = http_err.content.decode('utf-8', errors='ignore')
    except Exception:
      pass
    status = getattr(http_err, "status_code", None) or getattr(getattr(http_err, "resp", None), "status", None)
    logger.debug('ERROR OCCURRED: HttpError')
    logger.debug(f'Status: {status}')
    logger.debug(f'Content: {content}')
    raise Exception(f"Failed to create event: Google API error. {content or str(http_err)}") from http_err

  except Exception as error:
    import traceback
    logger.debug('ERROR OCCURRED: General')
    logger.debug(f'Error type: {type(error).__name__}')
    logger.debug(f'Error message: {str(error)}')
    logger.debug(f'Error traceback: {traceback.format_exc()}')
    raise Exception(f"Failed to create event: {str(error)}") from error

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
    mcp.run()  # FastMCP가 stdout(JSON-RPC)을 점유, 로그는 stderr로만 출력됨
  except KeyboardInterrupt:
    logger.info("Server stopped by user")
  except Exception as e:
    logger.error(f"Fatal error running server: {e}")
    sys.exit(1)

if __name__ == "__main__":
  main()