# 쿠리만쥬 데스크톱 업무메이트

Windows 10 1809 이상과 Windows 11을 대상으로 하는 Python 3.12 및 PySide6 6.8 앱입니다. 1단계에서는 투명한 항상 위 위젯, GIF 및 PNG 프레임 재생, 실시간 퇴근 말풍선을 제공합니다.

## Windows에서 실행

명령 프롬프트(cmd.exe)에서 이 폴더를 열고 실행하세요.

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run.py
```

원본 GIF를 `assets/character.gif`로 복사하거나, 원본 PNG 시퀀스를 `assets/frames/`에 복사하세요. 프레임은 파일명 숫자 순서로 120ms 간격으로 재생합니다. GIF는 원본 프레임 지연을 사용하며 계속 재생합니다. 파일명이나 폴더를 유지하려면 위젯을 우클릭해 **설정**에서 원본 경로를 선택하세요. 최초 실행 시 이미지가 없으면 말풍선에 파일 선택 안내가 표시됩니다.

위젯을 드래그하면 위치를 변경할 수 있고, 짧게 클릭하면 2초 동안 응원 문구가 표시됩니다. 우클릭 메뉴에서 크기, 출근, 점심, 퇴근을 설정하거나 종료할 수 있습니다. 기본 일정은 컴퓨터 현지 시간 기준으로 09:00 출근, 12:00~13:00 점심, 18:00 퇴근입니다. 현재 일정은 매일 반복되며 자정을 넘는 근무는 지원 범위 밖입니다.

## 구조

| 경로 | 역할 |
| --- | --- |
| `run.py` | 앱 실행과 사용자별 설정 연결 |
| `mate/schedule.py` | 시간대별 문구와 퇴근 카운트다운 계산 |
| `mate/animation.py` | 원본 GIF 및 투명 PNG 재생 |
| `mate/widget.py` | 투명 위젯, 말풍선, 입력 및 설정 |
| `assets/` | 사용자가 제공하는 원본 리소스 위치 |
| `tests/` | 시간 경계, 재생, 투명도, 입력 검사 |

## 테스트와 EXE 빌드

Windows 명령 프롬프트에서 다음 명령을 실행합니다.

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onedir --windowed --name KurimanjuMate run.py
```

Windows의 빌드 출력은 `dist/KurimanjuMate/KurimanjuMate.exe`입니다. 배포 시 `KurimanjuMate` 폴더 전체를 유지하세요. 자동 검색을 쓰려면 EXE 옆에 `assets`를 둡니다. 빌드 및 실행에는 관리자 권한이나 별도의 실행 정책 설정이 필요 없습니다.

실기기 검증에서는 EXE를 실행해 바탕화면 위 투명 합성, 원본 GIF 품질, 드래그, 크기 및 위치 복원을 확인합니다. 시간대별 캐릭터 행동과 Windows 자동실행 옵션은 후속 단계입니다.
