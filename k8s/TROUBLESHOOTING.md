# 볼륨 데이터 유지 문제 해결 가이드

## 문제: 파드 재시작 시 볼륨 데이터가 사라짐

### 증상
- 파드가 재시작될 때마다 `/app/skills`에 저장된 스킬이 사라짐
- 볼륨에 데이터가 저장되지 않거나, 파드 재시작 후 데이터가 없어짐

### 원인 진단

#### 1. 볼륨이 마운트되지 않았는지 확인

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

**문제가 있는 경우:**
- `Mounts:` 섹션에 `skills-storage`가 없음
- → Deployment에 볼륨 설정이 없거나 적용되지 않음

**해결:**
```bash
kubectl apply -f k8s/deployment.yaml
kubectl rollout restart deployment/claude-skills -n dev
```

#### 2. PVC 상태 확인

```bash
kubectl get pvc claude-skills-storage -n dev
```

**예상 상태:**
- `STATUS`: `Bound`
- `VOLUME`: PV 이름이 있어야 함

**문제가 있는 경우:**
- `STATUS`: `Pending` → 스토리지 클래스 문제
- `STATUS`: `Lost` → 볼륨 손실

#### 3. 볼륨 디렉토리 확인

```bash
# 디렉토리 존재 확인
kubectl exec $POD_NAME -n dev -- ls -la /app/skills

# 볼륨 마운트 확인
kubectl exec $POD_NAME -n dev -- df -h /app/skills
```

**예상 결과:**
```
total 28
drwxr-xr-x 4 root root  4096 Jan 26 05:14 .
drwxr-xr-x 1 root root  4096 Jan 26 07:27 ..
drwx------ 2 root root 16384 Jan 26 04:53 lost+found
drwxr-xr-x 5 root root  4096 Jan 26 05:22 uengine
```

**문제가 있는 경우:**
- `No such file or directory` → 볼륨이 마운트되지 않음
- 디렉토리는 있지만 `df -h`에서 다른 파일시스템 → 볼륨이 아닌 임시 디렉토리

#### 4. 스킬 파일 확인

```bash
kubectl exec $POD_NAME -n dev -- find /app/skills -type f -name "SKILL.md"
```

**예상 결과:**
```
/app/skills/uengine/quiz-generator/SKILL.md
/app/skills/uengine/global-investment-analyzer/SKILL.md
```

**문제가 있는 경우:**
- 파일이 없음 → 데이터가 저장되지 않았거나 손실됨

## 해결 방법

### 방법 1: Deployment 재적용 (가장 일반적)

```bash
# 1. Deployment 재적용
kubectl apply -f k8s/deployment.yaml

# 2. 롤아웃 확인
kubectl rollout status deployment/claude-skills -n dev

# 3. 새 파드 확인
kubectl get pods -n dev -l app=claude-skills

# 4. 볼륨 마운트 확인
POD_NAME=$(kubectl get pods -n dev -l app=claude-skills -o jsonpath='{.items[0].metadata.name}')
kubectl describe pod $POD_NAME -n dev | grep -A 5 "Mounts:"
```

### 방법 2: PVC 재생성 (데이터 손실 주의)

```bash
# 1. Deployment 스케일 다운
kubectl scale deployment claude-skills --replicas=0 -n dev

# 2. PVC 삭제 (데이터 손실!)
kubectl delete pvc claude-skills-storage -n dev

# 3. 새 PVC 생성
kubectl apply -f k8s/pvc.yaml

# 4. PVC가 Bound 상태가 될 때까지 대기
kubectl wait --for=condition=Bound pvc/claude-skills-storage -n dev --timeout=60s

# 5. Deployment 재시작
kubectl scale deployment claude-skills --replicas=1 -n dev
```

### 방법 3: 파드 강제 재시작

```bash
# 파드 삭제 (Deployment가 자동으로 재생성)
kubectl delete pod $POD_NAME -n dev

# 새 파드 확인
kubectl get pods -n dev -l app=claude-skills
```

## 예방 조치

### 1. Deployment 설정 확인

`k8s/deployment.yaml`에 다음이 포함되어 있는지 확인:

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

### 2. PVC 설정 확인

`k8s/pvc.yaml`에 `storageClassName`이 명시되어 있는지 확인:

```yaml
spec:
  storageClassName: standard-rwo  # 클러스터에 맞는 값
  accessModes:
    - ReadWriteOnce
```

### 3. 환경 변수 확인

Deployment에 `SKILLS_STORAGE_PATH` 환경 변수가 설정되어 있는지 확인:

```yaml
env:
  - name: SKILLS_STORAGE_PATH
    value: "/app/skills"
```

### 4. 정기 모니터링

```bash
# 스크립트로 정기 확인
#!/bin/bash
POD_NAME=$(kubectl get pods -n dev -l app=claude-skills -o jsonpath='{.items[0].metadata.name}')
if kubectl exec $POD_NAME -n dev -- test -d /app/skills; then
  echo "✅ 볼륨 마운트됨"
  SKILL_COUNT=$(kubectl exec $POD_NAME -n dev -- find /app/skills -type f -name "SKILL.md | wc -l)
  echo "📁 스킬 파일 수: $SKILL_COUNT"
else
  echo "❌ 볼륨이 마운트되지 않음!"
fi
```

## 체크리스트

파드 재시작 후 다음을 확인:

- [ ] PVC 상태가 `Bound`인가?
- [ ] 파드의 `Mounts:`에 `skills-storage`가 있는가?
- [ ] `/app/skills` 디렉토리가 존재하는가?
- [ ] `df -h /app/skills`가 PVC를 가리키는가?
- [ ] 스킬 파일들이 여전히 존재하는가?
- [ ] 애플리케이션 로그에 "Using skills storage path from environment: /app/skills"가 있는가?

## 추가 리소스

- [Kubernetes Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
- [GKE Persistent Disks](https://cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/gce-pd-csi-driver)
- [볼륨 초기화 문제 분석](./VOLUME_ISSUE_ANALYSIS.md)
