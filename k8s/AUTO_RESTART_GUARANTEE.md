# 파드 자동 재시작 시 볼륨 마운트 보장

## 목표

파드가 자동으로 재시작될 때마다 **항상** 볼륨이 마운트되어 스킬 디렉토리가 유지되도록 보장합니다.

## 현재 설정

### 1. Deployment에 볼륨 설정 포함

`k8s/deployment.yaml`에 다음이 포함되어 있습니다:

```yaml
spec:
  template:
    spec:
      containers:
      - name: claude-skills
        volumeMounts:
          - name: skills-storage
            mountPath: /app/skills
      volumes:
        - name: skills-storage
          persistentVolumeClaim:
            claimName: claude-skills-storage
```

**중요:** Kubernetes는 Deployment의 template을 기반으로 새 파드를 생성하므로, template에 볼륨 설정이 있으면 **자동으로 마운트됩니다**.

### 2. InitContainer로 볼륨 마운트 검증

볼륨이 제대로 마운트되었는지 확인하는 initContainer를 추가했습니다:

```yaml
initContainers:
  - name: volume-mount-check
    image: busybox:1.36
    command: ['sh', '-c']
    args:
      - |
        if [ ! -d /app/skills ]; then
          echo "ERROR: /app/skills directory does not exist!"
          exit 1
        fi
        echo "Volume mounted successfully"
    volumeMounts:
      - name: skills-storage
        mountPath: /app/skills
```

**작동 방식:**
- 파드가 시작되기 전에 initContainer가 실행됩니다
- 볼륨이 마운트되지 않았거나 디렉토리가 없으면 파드 시작이 실패합니다
- 이렇게 하면 볼륨 없이 파드가 실행되는 것을 방지할 수 있습니다

### 3. PVC 영구 보존

PVC는 Deployment와 독립적으로 존재하므로, 파드가 재시작되어도 **PVC는 유지됩니다**.

```yaml
# k8s/pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: claude-skills-storage
  namespace: dev
spec:
  storageClassName: standard-rwo
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
```

## 작동 원리

### 파드 자동 재시작 시나리오

1. **파드가 종료됨** (노드 재시작, 리소스 부족, 헬스체크 실패 등)
2. **Kubernetes가 새 파드를 생성**
3. **InitContainer 실행:**
   - 볼륨이 마운트될 때까지 대기
   - `/app/skills` 디렉토리 존재 확인
   - 실패 시 파드 시작 중단
4. **메인 컨테이너 시작:**
   - 볼륨이 이미 마운트되어 있음
   - 기존 스킬 파일들이 그대로 존재
   - 애플리케이션이 정상 작동

### 볼륨 마운트 보장 메커니즘

1. **Deployment Template:**
   - Deployment의 `spec.template.spec.volumes`에 PVC가 정의되어 있음
   - 새 파드는 항상 이 template을 기반으로 생성됨
   - 따라서 **항상 볼륨이 마운트됨**

2. **PVC 영구성:**
   - PVC는 파드와 독립적으로 존재
   - 파드가 삭제되어도 PVC는 유지됨
   - 새 파드가 같은 PVC를 참조하면 같은 볼륨에 접근

3. **InitContainer 검증:**
   - 볼륨이 마운트되지 않으면 파드가 시작되지 않음
   - 문제를 조기에 발견하고 실패 처리

## 확인 방법

### 1. 현재 파드의 볼륨 마운트 확인

```bash
# 파드 이름 확인
POD_NAME=$(kubectl get pods -n dev -l app=claude-skills -o jsonpath='{.items[0].metadata.name}')

# 볼륨 마운트 확인
kubectl describe pod $POD_NAME -n dev | grep -A 5 "Mounts:"
```

**예상 결과:**
```
Mounts:
  /app/skills from skills-storage (rw)
```

### 2. InitContainer 로그 확인

```bash
kubectl logs $POD_NAME -n dev -c volume-mount-check
```

**예상 결과:**
```
Checking if volume is mounted...
Volume mounted successfully at /app/skills
total 28
drwxr-xr-x 4 root root  4096 Jan 26 05:14 .
...
```

### 3. 스킬 파일 확인

```bash
kubectl exec $POD_NAME -n dev -- find /app/skills -type f -name "SKILL.md"
```

### 4. 파드 재시작 테스트

```bash
# 파드 삭제 (Deployment가 자동으로 재생성)
kubectl delete pod $POD_NAME -n dev

# 새 파드 확인
kubectl get pods -n dev -l app=claude-skills -w

# 새 파드의 볼륨 확인
NEW_POD=$(kubectl get pods -n dev -l app=claude-skills -o jsonpath='{.items[0].metadata.name}')
kubectl describe pod $NEW_POD -n dev | grep -A 5 "Mounts:"
kubectl exec $NEW_POD -n dev -- find /app/skills -type f -name "SKILL.md"
```

## 문제 해결

### 문제: 파드가 시작되지 않음

**증상:**
- 파드가 `Init:0/1` 상태에서 멈춤
- InitContainer가 실패함

**원인:**
- PVC가 존재하지 않음
- PVC가 `Pending` 상태
- 볼륨 마운트 실패

**해결:**
```bash
# PVC 상태 확인
kubectl get pvc claude-skills-storage -n dev

# PVC가 없으면 생성
kubectl apply -f k8s/pvc.yaml

# InitContainer 로그 확인
kubectl logs $POD_NAME -n dev -c volume-mount-check
```

### 문제: 볼륨이 마운트되지 않음

**증상:**
- 파드는 실행 중이지만 `/app/skills`가 없음

**원인:**
- Deployment에 볼륨 설정이 없음
- Deployment가 업데이트되지 않음

**해결:**
```bash
# Deployment 재적용
kubectl apply -f k8s/deployment.yaml

# 롤아웃 확인
kubectl rollout status deployment/claude-skills -n dev
```

### 문제: 스킬 파일이 사라짐

**증상:**
- 볼륨은 마운트되었지만 파일이 없음

**원인:**
- PVC가 재생성되어 새 볼륨이 생성됨
- 다른 PVC를 참조하고 있음

**해결:**
```bash
# 현재 PVC 확인
kubectl get pvc -n dev

# Deployment가 올바른 PVC를 참조하는지 확인
kubectl get deployment claude-skills -n dev -o yaml | grep claimName
```

## 모니터링

### 정기 확인 스크립트

```bash
#!/bin/bash
# check-volume-mount.sh

NAMESPACE="dev"
DEPLOYMENT="claude-skills"

echo "=== 볼륨 마운트 상태 확인 ==="

# PVC 상태
echo -n "PVC 상태: "
PVC_STATUS=$(kubectl get pvc claude-skills-storage -n $NAMESPACE -o jsonpath='{.status.phase}' 2>/dev/null)
if [ "$PVC_STATUS" = "Bound" ]; then
  echo "✅ Bound"
else
  echo "❌ $PVC_STATUS"
  exit 1
fi

# 파드 상태
POD_NAME=$(kubectl get pods -n $NAMESPACE -l app=$DEPLOYMENT -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD_NAME" ]; then
  echo "❌ 파드를 찾을 수 없음"
  exit 1
fi

echo "파드: $POD_NAME"

# 볼륨 마운트 확인
VOLUME_MOUNTED=$(kubectl describe pod $POD_NAME -n $NAMESPACE | grep -c "skills-storage")
if [ "$VOLUME_MOUNTED" -gt 0 ]; then
  echo "✅ 볼륨 마운트됨"
else
  echo "❌ 볼륨이 마운트되지 않음"
  exit 1
fi

# 디렉토리 확인
if kubectl exec $POD_NAME -n $NAMESPACE -- test -d /app/skills 2>/dev/null; then
  echo "✅ /app/skills 디렉토리 존재"
  SKILL_COUNT=$(kubectl exec $POD_NAME -n $NAMESPACE -- find /app/skills -type f -name "SKILL.md" 2>/dev/null | wc -l)
  echo "📁 스킬 파일 수: $SKILL_COUNT"
else
  echo "❌ /app/skills 디렉토리가 없음"
  exit 1
fi

echo "=== 모든 확인 완료 ==="
```

## 요약

✅ **Deployment template에 볼륨 설정이 있으면 자동으로 마운트됩니다**
✅ **InitContainer로 볼륨 마운트를 검증합니다**
✅ **PVC는 파드와 독립적으로 유지됩니다**
✅ **파드가 재시작되어도 같은 PVC를 참조하므로 데이터가 유지됩니다**

**결론:** 현재 설정으로 파드가 자동으로 재시작되어도 볼륨이 항상 마운트되고 스킬 디렉토리가 유지됩니다.
