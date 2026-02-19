# Kubernetes Deployment Guide

Claude Skills MCP Backend를 Kubernetes에 배포하는 방법을 안내합니다.

## 사전 요구사항

- Kubernetes 클러스터 (1.19+)
- kubectl 설정 완료
- PersistentVolume 지원 (스토리지 클래스)

## 배포 단계

### 1. PersistentVolumeClaim 생성

업로드된 스킬이 파드 재시작 후에도 유지되도록 PVC를 먼저 생성합니다:

```bash
kubectl apply -f k8s/pvc.yaml
```

PVC 상태 확인:
```bash
kubectl get pvc -n dev
```

### 2. Deployment 배포

```bash
kubectl apply -f k8s/deployment.yaml
```

배포 상태 확인:
```bash
kubectl get deployment -n dev claude-skills
kubectl get pods -n dev -l app=claude-skills
```

### 3. Service 배포 (선택사항)

```bash
kubectl apply -f k8s/service.yaml
```

## 영구 저장소 설정

### 중요: 업로드된 스킬 영구 저장

업로드된 스킬이 파드 재시작 후에도 유지되도록 하려면 **PersistentVolumeClaim이 필수**입니다.

**현재 설정:**
- PVC 이름: `claude-skills-storage`
- 마운트 경로: `/app/skills`
- 환경 변수: `SKILLS_STORAGE_PATH=/app/skills`
- 저장 용량: 1Gi (필요시 `k8s/pvc.yaml`에서 조정)

### 스토리지 클래스 설정

**중요:** GCP GKE에서는 `storageClassName`을 명시적으로 지정하는 것이 권장됩니다.

현재 설정된 값: `standard-rwo` (GCP GKE 기본값)

클러스터의 실제 스토리지 클래스를 확인:
```bash
kubectl get storageclass
```

다른 스토리지 클래스를 사용하려면 `k8s/pvc.yaml` 수정:

```yaml
spec:
  storageClassName: fast-ssd  # 원하는 스토리지 클래스
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
```

**주의:** 
- GKE Spot 인스턴스를 사용하는 경우, 노드가 변경될 때 ReadWriteOnce PVC가 제대로 마운트되지 않을 수 있습니다
- 이 경우 일반 노드 풀 사용을 고려하거나, ReadWriteMany 스토리지 클래스를 사용하세요

### 저장 용량 확장

```bash
# PVC 편집
kubectl edit pvc claude-skills-storage -n dev

# storage 값을 원하는 크기로 변경 (예: 5Gi)
```

## 설정 커스터마이징

### 환경 변수

`k8s/deployment.yaml`에서 환경 변수를 추가/수정할 수 있습니다:

```yaml
env:
  - name: SKILLS_STORAGE_PATH
    value: "/app/skills"
  - name: PYTHONUNBUFFERED
    value: "1"
```

### ConfigMap 사용

설정 파일을 ConfigMap으로 관리하려면:

```bash
# ConfigMap 생성
kubectl create configmap claude-skills-config \
  --from-file=config.json=config.example.json \
  -n dev

# deployment.yaml에 volumeMount 추가
```

## 모니터링

### 로그 확인

```bash
# 파드 로그
kubectl logs -f deployment/claude-skills -n dev

# 특정 파드 로그
kubectl logs -f <pod-name> -n dev
```

### Health Check

```bash
# 포트 포워딩
kubectl port-forward deployment/claude-skills 8765:8765 -n dev

# Health check
curl http://localhost:8765/health
```

## 백업 및 복원

### 스킬 데이터 백업

```bash
# PVC의 데이터를 tar로 백업
kubectl exec -it <pod-name> -n dev -- tar czf /tmp/skills-backup.tar.gz -C /app/skills .
kubectl cp <pod-name>:/tmp/skills-backup.tar.gz ./skills-backup.tar.gz -n dev
```

### 복원

```bash
# 백업 파일을 파드로 복사
kubectl cp ./skills-backup.tar.gz <pod-name>:/tmp/skills-backup.tar.gz -n dev

# 압축 해제
kubectl exec -it <pod-name> -n dev -- tar xzf /tmp/skills-backup.tar.gz -C /app/skills
```

## 스케일링

현재는 `replicas: 1`로 설정되어 있습니다. 여러 복제본을 사용하려면:

```yaml
spec:
  replicas: 3
```

**주의:** `ReadWriteOnce` PVC는 하나의 파드에서만 마운트할 수 있습니다. 여러 복제본을 사용하려면:
1. `ReadWriteMany` 스토리지 클래스 사용
2. 또는 공유 스토리지 (NFS 등) 사용

## 업데이트

```bash
# 이미지 업데이트
kubectl set image deployment/claude-skills \
  claude-skills=ghcr.io/uengine-oss/claude-skills:latest \
  -n dev

# 롤링 업데이트 확인
kubectl rollout status deployment/claude-skills -n dev
```

## 문제 해결

### PVC가 Pending 상태

```bash
# PVC 이벤트 확인
kubectl describe pvc claude-skills-storage -n dev

# 스토리지 클래스 확인
kubectl get storageclass
```

### 파드가 시작되지 않음

```bash
# 파드 상태 확인
kubectl describe pod <pod-name> -n dev

# 이벤트 확인
kubectl get events -n dev --sort-by='.lastTimestamp'
```

### 스킬이 사라짐

1. PVC가 제대로 마운트되었는지 확인:
   ```bash
   kubectl exec -it <pod-name> -n dev -- ls -la /app/skills
   ```

2. 환경 변수 확인:
   ```bash
   kubectl exec -it <pod-name> -n dev -- env | grep SKILLS_STORAGE_PATH
   ```

3. PVC가 삭제되지 않았는지 확인:
   ```bash
   kubectl get pvc -n dev
   ```

## 네임스페이스 변경

다른 네임스페이스에 배포하려면 모든 YAML 파일의 `namespace` 필드를 수정:

```bash
# 모든 파일에서 namespace 변경
sed -i 's/namespace: dev/namespace: production/g' k8s/*.yaml
```

## 리소스 요구사항

현재 설정:
- CPU: 50m 요청, 500m 제한
- Memory: 512Mi 요청, 1Gi 제한
- Storage: 1Gi (PVC)

필요시 `k8s/deployment.yaml`에서 조정 가능합니다.

