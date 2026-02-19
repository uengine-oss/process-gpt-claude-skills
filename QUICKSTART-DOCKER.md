# 🐳 Docker로 빠르게 시작하기

가장 빠르고 쉬운 방법으로 Claude Skills MCP Backend를 실행해보세요.

## 사전 요구사항

- Docker Desktop 설치 (https://www.docker.com/products/docker-desktop)
- 최소 2GB 여유 디스크 공간

## 5분 안에 실행하기

### 1️⃣ 프로젝트 다운로드

```bash
git clone https://github.com/K-Dense-AI/claude-skills-mcp.git
cd claude-skills-mcp
```

### 2️⃣ 서버 시작

```bash
docker-compose up -d
```

처음 실행 시 이미지를 다운로드하므로 1-2분 정도 소요됩니다.

### 3️⃣ 확인

```bash
# 서버 상태 확인
curl http://localhost:8765/health

# 또는 자동 테스트 스크립트
./test-docker.sh
```

성공적으로 실행되면 다음과 같은 응답을 받습니다:

```json
{
    "status": "ok",
    "version": "1.0.6",
    "skills_loaded": 123,
    "models_loaded": true,
    "loading_complete": true
}
```

## 🎯 이제 무엇을 할 수 있나요?

### MCP 클라이언트와 연결

백엔드 서버가 `http://localhost:8765/mcp`에서 MCP 프로토콜을 제공합니다.

### 사용 가능한 스킬 확인

123개의 과학 및 일반 스킬이 자동으로 로드됩니다:
- 15개 Anthropic 공식 스킬
- 108개 과학 연구용 스킬 (바이오인포매틱스, 화학정보학 등)

### 로그 확인

```bash
docker-compose logs -f
```

## 🛠️ 유용한 명령어

```bash
# 서버 중지
docker-compose down

# 서버 재시작
docker-compose restart

# 상태 확인
docker-compose ps

# 리소스 사용량 확인
docker stats claude-skills
```

## ⚙️ 커스터마이징

### 설정 파일 수정

1. `config.example.json`을 복사:
```bash
cp config.example.json config.json
```

2. `config.json` 편집 (GitHub 토큰 추가, 스킬 소스 변경 등)

3. 서버 재시작:
```bash
docker-compose restart
```

### 다른 포트 사용

`docker-compose.yml`에서 포트 변경:

```yaml
ports:
  - "9000:8765"  # 9000 포트 사용
```

## 🐛 문제 해결

### 포트 충돌

```
Error: port is already allocated
```

다른 포트를 사용하거나 충돌하는 프로세스를 중지하세요.

### 메모리 부족

Docker Desktop 설정에서 메모리를 최소 2GB로 증가시키세요.

### 이미지를 찾을 수 없음

```bash
# 이미지를 수동으로 다운로드
docker pull ghcr.io/uengine-oss/claude-skills:latest
```

## 📚 더 알아보기

- 상세 Docker 가이드: [DOCKER.md](DOCKER.md)
- 백엔드 문서: [packages/backend/README.md](packages/backend/README.md)
- 프로젝트 홈: [README.md](README.md)

## 🆘 도움이 필요하신가요?

GitHub Issues: https://github.com/K-Dense-AI/claude-skills-mcp/issues

