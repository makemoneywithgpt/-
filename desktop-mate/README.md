# 쿠리만쥬 데스크톱 업무메이트

투명 위젯에서 캐릭터를 재생하고 퇴근까지 남은 시간을 표시하는 1단계 MVP입니다. Windows 10 1809 이상 및 Windows 11의 x64 PC와 표준 CPython 3.12~3.15 x64를 대상으로 합니다.

## Windows 실기기에서 실행

1. ZIP을 쓰기 가능한 폴더에 압축 해제합니다. 이미 설치한 표준 Python 3.15 x64를 그대로 사용하세요. CMD 스크립트는 `py -3.15`, PATH의 `python`, `py -3` 순서로 확인합니다. free-threaded 빌드는 지원 범위 밖입니다.
2. 움직이는 원본 GIF를 `assets/character.gif`로 복사하거나, 투명 PNG 프레임 두 장 이상을 `assets/frames/`에 넣습니다. 예를 들어 `001.png`, `002.png` 순으로 120ms 간격으로 재생됩니다. GIF는 원본 프레임 지연을 사용합니다. GIF와 PNG 폴더가 모두 있으면 GIF가 우선입니다.
3. `run-windows.cmd`를 더블클릭합니다. 이 스크립트는 `%LOCALAPPDATA%\KMate\v`에 사용자별 가상환경을 만들고 `requirements.txt`의 PySide6 Essentials를 설치한 뒤 앱을 실행합니다. 최초 다운로드에는 인터넷이 필요합니다. OneDrive처럼 깊은 프로젝트 경로에서도 의존성 설치 경로를 짧게 유지하기 위해 이 위치를 사용합니다. 이전 버전이 프로젝트에 만든 `.venv`와 `.venv-windows`는 그대로 둡니다. 수동으로 실행하려면 CMD에서 `setup-windows.cmd`를 실행한 뒤 `"%LOCALAPPDATA%\KMate\v\Scripts\python.exe" run.py`를 입력합니다.

기존에 선택한 캐릭터 경로가 있거나 파일명을 유지해야 한다면 위젯을 우클릭해 **설정**에서 GIF 또는 PNG 프레임 폴더를 선택하세요. 원본 경로와 바이트는 유지됩니다. 단일 PNG는 정지 이미지로 표시됩니다. 원본에 하늘 같은 불투명 배경이 있으면 화면에도 그대로 표시됩니다. 앱 창의 투명도는 원본 이미지의 배경을 제거하지 않습니다.

기본 일정은 PC 현지 시간 기준 09:00 출근, 12:00~13:00 점심, 18:00 퇴근입니다. 일정은 매일 반복되며 당일 근무만 지원합니다. Windows에서 크기, 위치, 시간, 사용자가 선택한 리소스 경로는 Qt의 사용자별 설정으로 `HKEY_CURRENT_USER\Software\DesktopMate\Kurimanju`에 저장됩니다. 배포 폴더에는 개인 설정 INI가 들어가지 않으며 기본값은 앱 코드에 포함됩니다. 소스 실행과 EXE는 같은 사용자 설정을 읽습니다.

Python 3.15에서는 PySide6 Essentials 6.12.x와 PyInstaller 6.22.3 이상을 사용합니다. PySide6가 같은 버전의 Shiboken6를 설치합니다.

## Windows EXE 빌드

1. Windows PC에서 `build-windows.cmd`를 실행합니다. 스크립트가 PyInstaller를 설치하고 자동 테스트와 리소스 검사를 통과한 뒤 `dist\KurimanjuMate\KurimanjuMate.exe`를 빌드합니다. 기존 배포 폴더가 있다면 먼저 다른 이름이나 위치로 옮겨 주세요.
2. 배포 시 `dist\KurimanjuMate` 폴더 전체를 유지합니다. 원본 리소스는 EXE 옆 `assets`에 그대로 복사됩니다. 우클릭으로 선택한 외부 파일을 EXE와 함께 배포하려면 빌드 전에 위의 `assets` 구조에 복사하세요.
3. `bundle-manifest.json`에서 애니메이션 형식과 프레임 수, 포함된 각 리소스의 SHA-256, Windows 플랫폼 및 GIF 플러그인 경로, 설정 저장 위치를 확인합니다. 빌드 과정이 복사본의 해시와 플러그인 파일을 확인합니다. 원본이 없는 경우 빌드는 경고를 표시하고 매니페스트에 `missing`을 기록합니다. CMD에서 `"%LOCALAPPDATA%\KMate\v\Scripts\python.exe" tools\verify_bundle.py --bundle dist\KurimanjuMate`를 실행하면 배포 폴더의 기존 리소스를 덮어쓰지 않고 원본과 다시 대조합니다.

실행과 빌드에는 CMD와 Python을 사용하며 관리자 권한이 필요 없습니다.

## 실기기 확인 순서

- **소스 실행**에서 GIF 또는 PNG 프레임이 여러 번 반복되고, 원본의 투명 영역 뒤로 바탕화면이 보이는지 확인합니다.
- 퇴근 예정 시각을 아직 지나지 않은 시각으로 설정해 말풍선이 초 단위로 줄어드는지 확인합니다. 퇴근 시각이 지나면 “퇴근 시간이에요!”가 표시됩니다.
- **빌드된 EXE**에서도 같은 두 항목을 확인합니다. 패키징한 이미지를 검증하려면 우클릭 **설정**에서 배포 폴더의 `assets\character.gif` 또는 `assets\frames`를 직접 선택합니다. 이전에 저장한 외부 경로가 있을 수 있습니다.
- 크기와 퇴근 시각을 바꾼 뒤 우클릭 메뉴로 종료하고 EXE를 다시 열어 사용자 설정이 유지되는지 확인합니다.

## 프로젝트 구조

| 경로 | 역할 |
| --- | --- |
| `run.py`, `mate/` | 기존 MVP 실행 및 앱 로직 |
| `run-windows.cmd`, `setup-windows.cmd` | Windows 실행 및 의존성 설치 |
| `build-windows.cmd`, `tools/verify_bundle.py` | 빌드 및 리소스 검사 |
| `assets/` | 사용자 원본 리소스 |
| `tests/` | 카운트다운, 재생, 입력과 패키징 검사 |
