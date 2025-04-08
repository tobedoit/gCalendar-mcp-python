#!/usr/bin/env python
import os
import json
import sys
import logging
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

# .env 파일 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(
  level=logging.DEBUG,
  format='DEBUG: %(asctime)s - %(message)s',
  stream=sys.stderr
)
logger = logging.getLogger(__name__)

# MCP 서버 생성
mcp = FastMCP("Google Calendar MCP", dependencies=["python-dotenv", "google-api-python-client", "google-auth", "google-auth-oauthlib"])

# 환경 변수 확인
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REFRESH_TOKEN:
  logger.error("Error: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN environment variables are required")
  sys.exit(1)

@mcp.tool()
async def create_event(summary: str, start_time: str, end_time: str, description: str = None, 
                       location: str = None, attendees: list = None, reminders: dict = None) -> str:
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
  logger.debug(f'Creating calendar event with args: {locals()}')
  
  try:
    logger.debug('Creating OAuth2 client')
    # Google OAuth2 인증 클라이언트 생성
    creds = Credentials(
      None,  # 액세스 토큰은 None으로 설정하고 refresh 토큰으로 갱신
      refresh_token=GOOGLE_REFRESH_TOKEN,
      token_uri="https://oauth2.googleapis.com/token",
      client_id=GOOGLE_CLIENT_ID,
      client_secret=GOOGLE_CLIENT_SECRET
    )
    logger.debug('OAuth2 client created')
    
    logger.debug('Creating calendar service')
    calendar_service = build('calendar', 'v3', credentials=creds)
    logger.debug('Calendar service created')
    
    # 이벤트 객체 생성
    event = {
      'summary': summary,
      'start': {
        'dateTime': start_time,
        'timeZone': 'Asia/Seoul'
      },
      'end': {
        'dateTime': end_time,
        'timeZone': 'Asia/Seoul'
      }
    }
    
    # 선택적 필드 추가
    if description:
      event['description'] = description
    
    if location:
      event['location'] = location
      logger.debug(f'Location added: {location}')
    
    if attendees:
      event['attendees'] = [{'email': email} for email in attendees]
      logger.debug(f'Attendees added: {event["attendees"]}')
    
    # 알림 설정: 전달된 값이 없으면 기본적으로 10분 전 팝업 알림
    if reminders:
      event['reminders'] = reminders
      logger.debug(f'Custom reminders set: {json.dumps(reminders)}')
    else:
      event['reminders'] = {
        'useDefault': False,
        'overrides': [
          {'method': 'popup', 'minutes': 10}
        ]
      }
      logger.debug(f'Default reminders set: {json.dumps(event["reminders"])}')
    
    logger.debug('Attempting to insert event')
    response = calendar_service.events().insert(calendarId='primary', body=event).execute()
    logger.debug(f'Event insert response: {json.dumps(response)}')
    
    return f"Event created: {response.get('htmlLink', 'No link available')}"
    
  except Exception as error:
    logger.debug(f'ERROR OCCURRED:')
    logger.debug(f'Error type: {type(error).__name__}')
    logger.debug(f'Error message: {str(error)}')
    import traceback
    logger.debug(f'Error traceback: {traceback.format_exc()}')
    raise Exception(f"Failed to create event: {str(error)}")

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