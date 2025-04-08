# MCP Calendar

MCP(Model Context Protocol) 서버를 이용한 Google Calendar 이벤트 생성 도구입니다.


## 설치 방법

```bash
pip install mcp-calendar-python
```

## 기능

- Claude 데스크톱 앱에서 MCP 서버를 통해 Google Calendar 이벤트를 생성할 수 있습니다
- OAuth2를 통한 안전한 Google Calendar API 인증
- 이벤트 생성, 수정, 확인 기능 지원

## 시작하기

### 필수 요구사항
- Python 3.10 이상
- Google Cloud Console에서 생성한 OAuth 2.0 클라이언트 ID와 시크릿
- Claude 데스크톱 앱

### 설정방법 
1. Google Cloud 콘솔에서 프로젝트 및 OAuth 2.0 클라이언트 ID 생성:

- Google Cloud Console에서 새 프로젝트 생성
- API 및 서비스 > 사용자 인증 정보에서 OAuth 2.0 클라이언트 ID 생성
- Calendar API 활성화

2. 환경 변수 설정:
```
GOOGLE_CLIENT_ID="your_client_id"
GOOGLE_CLIENT_SECRET="your_client_secret"
GOOGLE_REFRESH_TOKEN="your_refresh_token"
```

3. Claude 데스크톱 앱에서 다음 설정 사용:
```json
{
  "mcp-calendar-python": {
    "command": "uvx",
    "args": [
      "mcp-calendar-python"
    ],
    "env": {
      "GOOGLE_CLIENT_ID": "your_client_id", 
      "GOOGLE_CLIENT_SECRET": "your_client_secret", 
      "GOOGLE_REFRESH_TOKEN": "your_refresh_token"
    }
  }
}
```
### 사용방법
Claude에게 다음과 같이 요청할 수 있습니다:

- "내일 오후 2시에 팀 미팅 일정을 추가해줘"
- "5월 15일 점심시간에 미팅 일정을 추가해줘"

### 라이선스
이 프로젝트는 MIT 라이선스 하에 배포됩니다.

