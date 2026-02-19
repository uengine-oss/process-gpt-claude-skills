# Docker Deployment Guide

Claude Skills MCP Backend를 Docker로 실행하는 방법을 안내합니다.

## 빠른 시작

### 1. Docker Compose로 실행 (권장)

#### Option A: 사전 빌드된 이미지 사용 (가장 빠름)

```bash
# GitHub Container Registry에서 이미지 받아서 실행
docker-compose up -d

# 로그 확인
docker-compose logs -f

# 상태 확인
docker-compose ps

# 중지
docker-compose down
```

#### Option B: 소스에서 빌드

```bash
# 이미지 빌드 및 컨테이너 시작 (ghcr.io/uengine-oss/claude-skills:latest로 빌드됨)
docker-compose up -d --build
```

### 2. Docker로 직접 실행

#### Option A: 사전 빌드된 이미지 사용

```bash
# GitHub Container Registry에서 이미지 다운로드
docker pull ghcr.io/uengine-oss/claude-skills:latest

# 컨테이너 실행
docker run -d -p 8765:8765 --name claude-skills ghcr.io/uengine-oss/claude-skills:latest
```

#### Option B: 소스에서 빌드

```bash
# 이미지 빌드 (프로젝트 루트에서, ghcr.io/uengine-oss/claude-skills:latest로 태그)
docker build -t ghcr.io/uengine-oss/claude-skills:latest -f packages/backend/Dockerfile .

# 컨테이너 실행
docker run -d -p 8765:8765 --name claude-skills ghcr.io/uengine-oss/claude-skills:latest

# 로그 확인
docker logs -f claude-skills

# 중지
docker stop claude-skills
docker rm claude-skills
```

## 테스트

### 자동 테스트 스크립트 실행

```bash
./test-docker.sh
```

### 수동 테스트

#### Health Check

```bash
curl http://localhost:8765/health | python3 -m json.tool
```

예상 출력:
```json
{
    "status": "ok",
    "version": "1.0.6",
    "skills_loaded": 123,
    "models_loaded": true,
    "loading_complete": true,
    "auto_update_enabled": true
}
```

#### MCP 엔드포인트

MCP 서버는 `http://localhost:8765/mcp`에서 실행됩니다.

## 설정 커스터마이징

### 환경 변수

`docker-compose.yml`에서 환경 변수를 추가할 수 있습니다:

```yaml
environment:
  - PYTHONUNBUFFERED=1
  # 여기에 추가 환경 변수 설정
```

### 설정 파일

`config.example.json`을 수정하여 사용자 정의 설정을 적용할 수 있습니다:

```bash
# 설정 파일 복사 및 수정
cp config.example.json config.json
# config.json 편집...

# docker-compose.yml이 자동으로 /app/config.json으로 마운트
docker-compose up -d
```

주요 설정 옵션:
- `skill_sources`: 스킬 소스 (GitHub, 로컬)
- `embedding_model`: 임베딩 모델 선택
- `auto_update_enabled`: 자동 업데이트 활성화
- `github_api_token`: GitHub API 토큰 (선택사항, rate limit 증가)

### 업로드된 스킬 영구 저장 (중요!)

**업로드된 스킬이 컨테이너 재시작 후에도 유지되도록 하려면 영구 볼륨이 필요합니다.**

`docker-compose.yml`에는 이미 영구 볼륨이 설정되어 있습니다:

```yaml
volumes:
  - claude-skills-data:/app/skills  # Named volume (영구 저장)
```

이 볼륨은 `claude-skills-data`라는 이름의 Docker 볼륨으로, 컨테이너가 삭제되어도 데이터가 유지됩니다.

**환경 변수 설정:**
- `SKILLS_STORAGE_PATH=/app/skills` (이미 설정됨)
- 이 환경 변수가 설정되면 업로드된 스킬이 이 경로에 저장됩니다.

**확인 방법:**
```bash
# 볼륨 확인
docker volume ls | grep claude-skills-data

# 볼륨 내용 확인
docker run --rm -v claude-skills-data:/data alpine ls -la /data
```

### 로컬 스킬 디렉토리 마운트 (읽기 전용)

기존 로컬 스킬을 읽기 전용으로 마운트하려면:

```yaml
volumes:
  - ./config.example.json:/app/config.json:ro
  - ~/.claude/skills:/app/skills:ro  # 읽기 전용 마운트
```

**주의:** 읽기 전용(`:ro`) 마운트는 업로드 기능을 사용할 수 없습니다. 업로드 기능을 사용하려면 위의 영구 볼륨 방식을 사용하세요.

## 리소스 요구사항

- **CPU**: 2 cores 권장
- **메모리**: 최소 1GB, 권장 2GB
- **디스크**: 약 500MB (이미지) + 100MB (캐시)

실제 사용량:
```
CONTAINER          CPU %     MEM USAGE / LIMIT
claude-skills-mcp  0.23%     576MB / 7.66GB
```

## 문제 해결

### 컨테이너가 시작되지 않음

```bash
# 로그 확인
docker logs claude-skills

# 컨테이너 재시작
docker-compose restart
```

### Health Check 실패

```bash
# 컨테이너 상태 확인
docker inspect claude-skills | grep -A 20 "Health"

# 수동 health check
docker exec claude-skills curl localhost:8765/health
```

### 포트 충돌

다른 포트를 사용하려면 `docker-compose.yml` 수정:

```yaml
ports:
  - "9000:8765"  # 호스트 포트 9000 사용
```

### 이미지 재빌드

캐시 없이 완전히 재빌드:

```bash
docker-compose build --no-cache
docker-compose up -d
```

## 프로덕션 배포

### 보안 고려사항

1. **네트워크 격리**: 필요한 경우에만 포트 노출
```yaml
ports:
  - "127.0.0.1:8765:8765"  # 로컬에서만 접근
```

2. **리소스 제한**:
```yaml
deploy:
  resources:
    limits:
      cpus: '2'
      memory: 2G
    reservations:
      cpus: '1'
      memory: 1G
```

3. **로그 관리**:
```yaml
logging:
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"
```

### 모니터링

```bash
# 실시간 모니터링
docker stats claude-skills

# Health check
watch -n 5 'curl -s http://localhost:8765/health | python3 -m json.tool'
```

## 업데이트

```bash
# 최신 코드 가져오기
git pull

# 이미지 재빌드
docker-compose build

# 컨테이너 재시작
docker-compose up -d
```

## 백업

중요한 데이터는 볼륨으로 마운트하여 백업:

```bash
# 설정 파일 백업
cp config.json config.json.backup

# 업로드된 스킬 백업 (중요!)
docker run --rm -v claude-skills-data:/data -v $(pwd):/backup alpine tar czf /backup/skills-backup.tar.gz -C /data .

# 캐시 백업 (선택사항)
docker cp claude-skills:/tmp/claude_skills_mcp_cache ./cache_backup
```

### 복원

```bash
# 스킬 복원
docker run --rm -v claude-skills-data:/data -v $(pwd):/backup alpine tar xzf /backup/skills-backup.tar.gz -C /data
```

## 이미지 정보

### GitHub Container Registry

공식 이미지가 GitHub Container Registry에 호스팅됩니다:

- **Latest**: `ghcr.io/uengine-oss/claude-skills:latest`
- **버전별**: `ghcr.io/uengine-oss/claude-skills:1.0.6`

### 이미지 사양

- **베이스 이미지**: python:3.12-slim
- **크기**: ~2.34GB (PyTorch CPU 포함)
- **아키텍처**: linux/amd64
- **엔트리포인트**: claude-skills-mcp-backend (내부 실행 명령)

### 사용 가능한 태그

```bash
# 최신 버전 (권장)
docker pull ghcr.io/uengine-oss/claude-skills:latest

# 특정 버전
docker pull ghcr.io/uengine-oss/claude-skills:1.0.6
```

## 참고

- 백엔드 README: [packages/backend/README.md](packages/backend/README.md)
- 메인 문서: [README.md](README.md)
- GitHub Container Registry: https://github.com/orgs/uengine-oss/packages/container/claude-skills

## 지원

문제가 발생하면 GitHub Issues에 보고해주세요:
https://github.com/K-Dense-AI/claude-skills-mcp/issues

