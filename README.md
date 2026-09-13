# 🤟 VRChat 한국수어교실 디스코드 조교 봇: '이미숫' (Leemisut Bot)

<div align="center">

<!-- 프로필 이미지 (assets/leemisut_profile.png 경로에 이미지를 올린 후 연동) -->
<img src="./assets/leemisut_profile.png" width="180" height="180" alt="조교 이미숫 프로필" style="border-radius: 50%;">

> **"오늘도 한 단어씩, 천천히 같이 익혀 봐요!"**  
> VRChat 한국수어교실 서버를 위한 교육적이고 유쾌한 수어 학습 & 퀴즈 디스코드 봇입니다.

![Python Version](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python)
![discord.py](https://img.shields.io/badge/discord.py-2.4%2B-5865F2?logo=discord)
![License](https://img.shields.io/badge/Code_License-MIT-green.svg)
![Brand Copyright](https://img.shields.io/badge/Brand-CC%20BY--NC--ND%204.0-orange.svg)

</div>

## ✨ 주요 기능
- **🤟 /오늘의수어**: 날짜(KST)를 기준으로 매일 새로운 한국수어 단어 및 수어 설명/동영상 카드 전송
- **🧩 /수어퀴즈**: 3,754개 수어 DB 기반 수어 동작 영상 보고 단어 맞추기 (버튼 UI, 스포일러 방지, 포인트/EXP 게이머화)
- **📜 /명언**: 일상 속 격언, 필적확인란 문구, 사자성어로 마음속 수어 번역 생각하기
- **⚡ /수어전체동기화**: 문화공공데이터광장 API 기반 3,700여 개 수어 로컬 DB 자동 구축 (관리자 전용)

## 🛠️ 기술 스택 (Tech Stack)
- **Language**: Python 3.12+
- **Framework**: `discord.py` v2.x (Slash Commands, Discord UI Components)
- **Database**: `aiosqlite` (Async SQLite3)
- **Open API**: 국립국어원 한국수어사전 / 문화공공데이터광장 API
> 본 프로젝트는 국립국어원(한국수어사전) 및 문화공공데이터광장의 Open API 데이터를 활용하여 제작되었습니다.
- **AI Collaborators**: Gemini (Architecture & Specification), Claude (Core Code Implementation)

## 🏗️ 깃 복제 (Git clone)
```bash
git clone [https://github.com/LeeSimYul/Leemisut-bot.git](https://github.com/LeeSimYul/Leemisut-bot.git)
cd Leemisut-bot
```

## 📜 라이선스 및 저작권 (License & Copyright)
- **Code Engine:** Licensed under the [MIT License](./LICENSE).
- **Brand & Assets:** Copyright (c) 2026 LeeSimYul. All rights reserved.
- 본 프로젝트는 코드와 캐릭터 자산에 대해 라이선스(License)를 적용합니다.
  - Source Code: MIT License에 따라 소스코드의 자유로운 참고 및 재사용이 가능합니다.
  - Brand & Persona: '조교 이미숫(Leemisut)' 캐릭터 이름, 프로필 이미지, 고유 대사 템플릿의 소유권은 이심율(LeeSimYul) 및 VRChat 한국수어교실에 있으며 무단 상업적 도용을 금지합니다.
