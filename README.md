# 🤟 VRChat 한국수어교실 보조선생님 디스코드 봇: '이미숫' (Leemisut Bot)

<div align="center">

<!-- 프로필 이미지 (assets/leemisut_profile.png 경로에 이미지를 올린 후 연동) -->
<img src="./assets/leemisut_profile.png" width="180" height="180" alt="조교 이미숫 프로필" style="border-radius: 50%;">

> **"오늘도 한 단어씩, 천천히 같이 익혀 봐요!"**  
> VRChat 한국수어교실 서버를 위한 교육적이고 유쾌한 수어 학습 & 퀴즈 디스코드 봇입니다.

![Python Version](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python)
![discord.py](https://img.shields.io/badge/discord.py-2.4%2B-5865F2?logo=discord)
![Database](https://img.shields.io/badge/aiosqlite-WAL_mode-003B57?logo=sqlite)
![License](https://img.shields.io/badge/Code_License-MIT-green.svg)
![Brand Copyright](https://img.shields.io/badge/Brand-CC%20BY--NC--ND%204.0-orange.svg)

</div>

## 📖 프로젝트 소개

**이미숫 (Leemisut Bot)** 은 VRChat 한국수어교실에서 **보조선생님** 역할을 맡은 디스코드 봇입니다.
국립국어원 한국수어사전의 일상생활수어 자료(약 3,700건)를 로컬 DB에 담아 두고, 디스코드 안에서 수어 공부를 이어 갈 수 있게 돕습니다.

- 📅 매일 **나에게 맞춘 단어**로 출석하고
- 🧩 **퀴즈**로 확인하고, 틀린 단어는 **오답 복습**으로 다시 만나고
- 📒 마음에 드는 단어는 **나만의 단어장**에 모아 두고
- 👤 쌓인 **레벨 · 포인트 · 연속 출석**을 프로필로 확인합니다.

> 💬 서버에서 `/안녕` 을 입력하면 언제든 이미숫 조교의 명령어 안내판을 볼 수 있어요.

## ☕ 이미숫 가이드라인(Notion)
- 🤖 **[Discord Bot 이미숫 공식 설명서](https://likeable-bucket-c21.notion.site/Discord-Bot-c7dc1254368c4a4fbcdf2e1ef6ca23a4?source=copy_link)**
- 📜 **[Discord Bot 이미숫 서비스 이용 약관](https://likeable-bucket-c21.notion.site/Discord-Bot-27213199bb4f43f49d679a2842c87c1c?source=copy_link)**
- 📃 **[Discord Bot 이미숫 개인정보 보호 정책](https://likeable-bucket-c21.notion.site/Discord-Bot-3d9a401b5418801c9f0afbcae52fca29?source=copy_link)**

## ✨ 주요 기능

### 🤟 수어 학습
| 명령어 | 설명 |
|---|---|
| `/오늘의수어` | 유저마다 다른 **1일 1회 맞춤 단어**(KST 기준)를 수어 설명 · 영상 · 사전 링크와 함께 보여 줍니다. 하루 첫 확인 시 **출석 보상**(+5 pt · +5 exp)과 **연속 출석일수**가 쌓입니다. |
| `/수어검색` | **단어명 · 분야(분류)** 조건으로 수어사전을 검색합니다. 자동완성을 지원하며, 결과가 1건이면 상세 카드로, 여러 건이면 **5개씩 페이지 버튼**으로 넘겨 봅니다. |

### 🧩 퀴즈 & 복습
| 명령어 | 설명 |
|---|---|
| `/수어퀴즈` | 수어 동작(사진 · 영상)을 보고 4지선다 버튼에서 정답을 고릅니다. (제한 45초, 정답 시 +10 pt · +10 exp) **스마트 오답 복습**: 30% 확률로 '틀린 뒤 아직 못 맞힌 단어'가 복습 문제로 출제됩니다. 정답 공개 전에는 단어명이 드러나는 링크를 숨깁니다. |
| `/단어장저장` | 단어를 **나만의 수어 단어장**에 담거나 뺍니다. (토글) 자동완성으로 '배(과일)' · '배(신체)' 같은 동음이의어도 뜻별로 정확히 고를 수 있습니다. |
| `/수어단어장` | 담아 둔 단어를 최근에 담은 순서로 **5개씩 페이지 버튼**으로 넘겨 봅니다. 단어마다 영상 · 사전 링크가 붙어 있고, 나에게만 보입니다. |

### 📜 표현력 향상
| 명령어 | 설명 |
|---|---|
| `/명언` · `/격언` | 역대 수능 필적확인란 · 사자성어 · 문학 · 명언 속 한 문장과 핵심 단어를 보여 주고, **이 문장을 수어로 어떻게 옮길지** 생각해 보게 합니다. 갈래를 골라 볼 수도 있어요. |

### 👤 개인 프로필
| 명령어 | 설명 |
|---|---|
| `/내정보` | **레벨**(경험치 100마다 +1) · **경험치** 진행 막대 · **보유 포인트** · **연속 출석일수** · **단어장 개수**를 한 장의 카드로 보여 줍니다. |

### 🛠️ 관리자 전용
| 명령어 | 설명 |
|---|---|
| `/수어동기화` | 검색어(쉼표로 최대 5개)로 수어사전 Open API에서 자료를 받아 저장합니다. 저장하지 않고 결과만 보는 미리보기 모드를 지원합니다. |
| `/수어전체동기화` | 일상생활수어 전체를 페이지 단위로 순회하며 DB를 구축합니다. 진행률이 실시간으로 표시됩니다. |
| `/수어삭제` | 잘못 저장된 단어를 삭제합니다. 자동완성을 지원하며, 동음이의어가 함께 지워지면 무엇이 지워졌는지 알려 줍니다. |
| `/수어목록` | 저장된 단어 전체 또는 검색 결과를 **10개씩 페이지 버튼**으로 확인합니다. |

> 🔐 `/수어동기화` · `/수어전체동기화` · `/수어삭제` 는 **서버 관리자 권한 + `ADMIN_USER_IDS` 목록** 2중 검증을 모두 통과해야 실행됩니다. `/수어목록` 은 서버 관리자 권한만 확인합니다.

## 🧠 기술적 특징 (Technical Highlights)

- **discord.py v2.x Async & Slash Commands**
  모든 슬래시 명령어는 시작 직후 `defer()` 로 응답을 미뤄, 디스코드의 3초 응답 제한(`10062 Unknown Interaction`)에 걸리지 않습니다. 기능별 Cog(`cogs/`)는 봇이 시작할 때 자동으로 로드됩니다.
- **aiosqlite WAL 모드 및 PRAGMA 최적화**
  `journal_mode=WAL` 로 읽기와 쓰기가 서로를 막지 않고, `synchronous=NORMAL` 로 커밋마다 디스크 동기화를 하지 않아 쓰기가 빠릅니다. LIKE 검색은 `%` · `_` 와일드카드 이스케이프와 검색어 50자 제한으로 패턴 폭주(DoS)를 막습니다.
- **인메모리 캐싱 (In-Memory Caching)**
  전체 단어 수 · 분류 목록처럼 자주 읽는 집계값은 메모리에 보관해 즉시 돌려줍니다. 동기화 · 삭제가 일어나면 캐시가 자동으로 비워지고, 조회 도중 데이터가 바뀐 경우에는 옛값을 저장하지 않도록 세대(generation) 검사를 합니다.
- **클릭형 페이지네이션 (`SignPaginatorView`)**
  `discord.ui.View` 기반의 `◀ 이전 · 1 / N 페이지 · ▶ 다음` 버튼 컴포넌트입니다. 페이지는 필요할 때만 DB에서 불러오고(LIMIT/OFFSET) 한 번 본 페이지는 다시 쓰며, 명령어를 실행한 사람만 조작할 수 있고 마지막 조작 60초 뒤 버튼이 자동으로 잠깁니다. (`utils/paginator.py`)
- **보안: 유저별 쿨다운 & 관리자 2중 검증**
  일반 명령어는 유저당 3초에 1회로 제한(Rate Limit)되고, 초과하면 본인에게만 안내가 보입니다. 관리자 명령어는 `has_permissions(administrator=True)` 와 `.env` 의 `ADMIN_USER_IDS` 를 함께 확인하며, 목록이 잘못 적혀 있으면 관리자 명령어가 잠기는 fail-closed 방식입니다. 목록 밖 사용자의 시도는 로그로 남습니다.
- **개인화 학습 데이터**
  `quiz_logs`(퀴즈 풀이 기록)와 `user_bookmarks`(단어장) 테이블로 유저별 학습 이력을 관리합니다. 오답 복습은 '마지막 풀이가 오답인 단어'를 많이 틀린 순으로 골라 출제하며, 기존 DB는 봇 시작 시 데이터를 유지한 채 자동으로 마이그레이션됩니다.

## 🛠️ 기술 스택 (Tech Stack)
- **Language**: Python 3.12+
- **Framework**: `discord.py` v2.x (Slash Commands, Discord UI Components)
- **Database**: `aiosqlite` (Async SQLite3, WAL mode)
- **Network / Config**: `aiohttp`, `python-dotenv`
- **Open API**: 국립국어원 한국수어사전 / 문화공공데이터광장 API
> 본 프로젝트는 국립국어원(한국수어사전) 및 문화공공데이터광장의 Open API 데이터를 활용하여 제작되었습니다.

## 🗂️ 프로젝트 구조
```text
Leemisut-bot/
├── main.py                # 엔트리 포인트 (Cog 자동 로드 · DB 연결 · 슬래시 명령어 동기화)
├── database.py            # aiosqlite DB 계층 (스키마 · 마이그레이션 · 캐시 · 퀴즈 기록 · 단어장)
├── cogs/
│   ├── general.py         # /안녕 · /명언 · /격언 · /내정보
│   ├── sign_language.py   # /오늘의수어 · /수어검색 · /수어퀴즈 · /단어장저장 · /수어단어장
│   └── admin.py           # /수어동기화 · /수어전체동기화 · /수어삭제 · /수어목록 (관리자)
├── utils/
│   ├── ksl_api.py         # 수어사전 Open API 클라이언트 (수집 · 검증 필터)
│   └── paginator.py       # SignPaginatorView (공용 페이지 버튼 UI)
├── assets/                # 프로필 이미지
├── data/                  # SQLite DB (처음 실행 시 자동 생성 · Git 제외)
├── .env.example           # 환경 변수 예시
└── requirements.txt
```

## 🚀 실행 가이드

### 0. 준비물
- **Python 3.12 이상**
- **디스코드 봇 토큰**: [Discord Developer Portal](https://discord.com/developers/applications)에서 봇을 만든 뒤, **Bot → Privileged Gateway Intents** 에서 `MESSAGE CONTENT` 와 `SERVER MEMBERS` 를 켜 주세요. 서버에 초대할 때는 `bot` · `applications.commands` 권한 범위(scope)가 필요합니다.
- **(동기화용) 수어 API 인증키**: [문화공공데이터광장](https://www.culture.go.kr/data)에서 발급받은 인증키가 있어야 `/수어동기화` · `/수어전체동기화` 로 실제 자료를 받아올 수 있습니다.

### 1. 저장소 복제 & 패키지 설치
```bash
git clone https://github.com/LeeSimYul/Leemisut-bot.git
cd Leemisut-bot
python -m venv venv
```

가상환경을 켠 뒤 패키지를 설치합니다. (Windows: `venv\Scripts\activate` · macOS/Linux: `source venv/bin/activate`)

```bash
pip install -r requirements.txt
```

### 2. 환경 변수 설정 (`.env`)
`.env.example` 을 복사해 `.env` 를 만들고 값을 채워 주세요. (Windows: `copy .env.example .env`)

```bash
cp .env.example .env
```

| 변수 | 필수 | 설명 |
|---|:---:|---|
| `DISCORD_TOKEN` | ✅ | 디스코드 봇 토큰 |
| `KSL_API_KEY` | 동기화 시 | 문화공공데이터광장 수어 API 인증키 |
| `DEV_GUILD_ID` | 선택 | 테스트 서버 ID. 넣으면 그 서버에 슬래시 명령어가 즉시 반영됩니다. |
| `ADMIN_USER_IDS` | 선택 | 봇 관리자 디스코드 유저 ID (쉼표로 여러 명). 비우면 서버 관리자 권한만 확인합니다. |
| `DB_PATH` | 선택 | DB 파일 경로 (기본값: `data/imisut.db`) |
| `KSL_API_ENDPOINT` | 선택 | 다른 수어 API를 쓸 때만 지정 (기본값: 국립국어원 일상생활수어) |

> 💡 선택 항목은 `.env.example` 에 주석(`#`)으로 들어 있습니다. 쓰려면 맨 앞의 `#` 을 지우고 값을 넣어 주세요. (`DEV_GUILD_ID` 는 반드시 숫자로 된 서버 ID여야 합니다)  
> ⚠️ `.env` 에는 봇 토큰이 들어 있으므로 **절대 커밋하지 마세요.** (`.gitignore` 에 이미 포함되어 있습니다)

### 3. 봇 실행
```bash
python main.py
```

아래와 같은 로그가 보이면 정상입니다.
```text
🗄️ DB 연결 완료: .../data/imisut.db
🧩 Cog 로드 완료: cogs.admin / cogs.general / cogs.sign_language
🔗 전역 슬래시 명령어 N개 동기화
✨ 수어 연구학 조교 이미숫(이)가 성공적으로 디스코드에 접속했습니다!
```

### 4. 첫 설정
1. 처음 실행하면 `data/imisut.db` 가 자동으로 만들어지고 테스트용 단어 4개가 들어갑니다.
2. 관리자 계정으로 `/수어전체동기화` 를 실행하면 실제 수어 자료(약 3,700건)가 채워집니다.
3. `DEV_GUILD_ID` 없이 전역으로 동기화하면 슬래시 명령어가 목록에 뜨기까지 최대 1시간쯤 걸릴 수 있습니다.

## 🔒 데이터 & 보안 안내
- 유저의 포인트 · 출석 · 퀴즈 기록 · 단어장은 서버 컴퓨터의 `data/` 폴더 SQLite DB에만 저장됩니다.
- `data/` 폴더와 `*.db` · `*.db-wal` · `*.db-shm` 파일, `.env` 는 `.gitignore` 로 GitHub에 올라가지 않도록 막혀 있습니다.
- 개인정보 처리 방침은 위 [이미숫 가이드라인(Notion)](#-이미숫-가이드라인notion)의 개인정보 보호 정책을 참고해 주세요.

## 📜 라이선스 및 저작권 (License & Copyright)
- **Code Engine:** Licensed under the [MIT License](./LICENSE).
- **Brand & Assets:** Copyright (c) 2026 LeeSimYul. All rights reserved.
- 본 프로젝트는 코드와 캐릭터 자산에 대해 라이선스(License)를 적용합니다.
  - Source Code: MIT License에 따라 소스코드의 자유로운 참고 및 재사용이 가능합니다.
  - Brand & Persona: '조교 이미숫(Leemisut)' 캐릭터 이름, 프로필 이미지, 고유 대사 템플릿의 소유권은 이심율(LeeSimYul) 및 VRChat 한국수어교실에 있으며 무단 상업적 도용을 금지합니다.

## 🤝 기술 협업 (Credits)
- **Author**: 이심율 ([LeeSimYul](https://github.com/LeeSimYul)) — 기획 · 운영 · VRChat 한국수어교실
- **AI Collaborators**: Gemini (기획 / 아키텍처 설계), Claude (코드 구현)
- **Data**: 국립국어원 한국수어사전 · 문화공공데이터광장 Open API
