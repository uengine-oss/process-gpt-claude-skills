# 볼륨 초기화 문제 분석 및 해결 방안

## 문제 현상

- GCP 클러스터의 dev 네임스페이스에 deployment, service, PVC가 모두 생성되어 있음
- 파드가 간헐적으로 재시작됨
- rollout으로 재시작할 때마다 마운트된 볼륨에 저장된 스킬이 초기화되는 것 같음

## 원인 분석

### 1. PVC에 storageClassName이 명시되지 않음 ⚠️ **주요 원인**

**문제점:**
- `pvc.yaml`에 `storageClassName`이 없음
- GCP GKE에서는 기본 스토리지 클래스가 클러스터 설정에 따라 다를 수 있음
- 명시하지 않으면 기본값에 의존하게 되어 바인딩이 실패하거나 잘못된 스토리지 클래스를 사용할 수 있음

**해결:**
```yaml
spec:
  storageClassName: standard-rwo  # GCP GKE 기본값 (또는 클러스터에 맞는 값)
```

### 2. GKE Spot 인스턴스와 ReadWriteOnce PVC의 호환성 문제

**문제점:**
- Deployment가 `cloud.google.com/gke-spot: "true"` 노드 셀렉터를 사용
- Spot 인스턴스는 노드가 자주 변경될 수 있음
- `ReadWriteOnce` PVC는 한 노드에만 마운트 가능
- 파드가 다른 노드에서 시작되면 이전 노드에 마운트된 볼륨에 접근할 수 없음

**영향:**
- 파드가 다른 노드에서 시작될 때 볼륨이 마운트되지 않아 빈 디렉토리로 시작할 수 있음
- 또는 볼륨 바인딩이 실패할 수 있음

**해결 방안:**
1. **권장**: Spot 인스턴스 대신 일반 노드 풀 사용 (볼륨 안정성 우선)
2. **대안**: `ReadWriteMany` 스토리지 클래스 사용 (NFS 등)
3. **현실적**: 현재 설정 유지하되, PVC가 제대로 바인딩되는지 모니터링

### 3. 볼륨 마운트 타이밍 문제

**문제점:**
- 파드가 시작될 때 볼륨이 아직 준비되지 않았을 수 있음
- 애플리케이션이 시작되면서 빈 디렉토리를 생성할 수 있음

**코드 분석:**
- `http_server.py`의 `_get_primary_local_skill_root()` 함수에서:
  ```python
  path.mkdir(parents=True, exist_ok=True)
  ```
  - 이 코드는 디렉토리가 없으면 생성하지만, 기존 내용을 삭제하지는 않음
  - 하지만 볼륨이 마운트되지 않으면 빈 디렉토리로 시작됨

**해결:**
- Kubernetes는 기본적으로 볼륨이 마운트될 때까지 파드 시작을 대기함
- 추가 확인이 필요하면 initContainer를 사용할 수 있음

## 적용된 수정 사항

### 1. PVC에 storageClassName 추가

```yaml
# k8s/pvc.yaml
spec:
  storageClassName: standard-rwo  # GCP GKE 기본 스토리지 클래스
  accessModes:
    - ReadWriteOnce
```

**주의:** 클러스터의 실제 스토리지 클래스 이름을 확인하고 맞춰야 합니다:
```bash
kubectl get storageclass
```

### 2. Deployment 정리

- 중복된 volumes 섹션 제거
- 볼륨 마운트 설정 확인

## 추가 확인 사항

### 1. PVC 상태 확인

```bash
# PVC 상태 확인
kubectl get pvc claude-skills-storage -n dev

# PVC 상세 정보
kubectl describe pvc claude-skills-storage -n dev

# PV 확인
kubectl get pv
```

**예상 상태:**
- `STATUS`가 `Bound`여야 함
- `VOLUME`에 PV 이름이 있어야 함

### 2. 파드 볼륨 마운트 확인

```bash
# 파드 이름 확인
kubectl get pods -n dev -l app=claude-skills

# 파드 상세 정보 (볼륨 마운트 확인)
kubectl describe pod <pod-name> -n dev

# 파드 내부에서 볼륨 확인
kubectl exec -it <pod-name> -n dev -- ls -la /app/skills
```

### 3. 스토리지 클래스 확인

```bash
# 사용 가능한 스토리지 클래스 확인
kubectl get storageclass

# 기본 스토리지 클래스 확인
kubectl get storageclass -o jsonpath='{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")].metadata.name}'
```

### 4. 볼륨 데이터 확인

```bash
# 파드 내부에서 스킬 파일 확인
kubectl exec -it <pod-name> -n dev -- find /app/skills -type f

# 볼륨에 실제로 데이터가 있는지 확인
kubectl exec -it <pod-name> -n dev -- du -sh /app/skills
```

## 문제 해결 체크리스트

- [ ] PVC에 올바른 `storageClassName` 설정
- [ ] PVC 상태가 `Bound`인지 확인
- [ ] 파드가 볼륨을 제대로 마운트했는지 확인
- [ ] 파드 재시작 후에도 볼륨 데이터가 유지되는지 확인
- [ ] Spot 인스턴스 사용 시 노드 변경으로 인한 문제 모니터링

## 해결 완료 ✅

### 발견된 문제

1. **Deployment에 볼륨이 마운트되지 않음** ⚠️ **주요 원인**
   - deployment.yaml 파일에는 볼륨 설정이 있었지만, 클러스터에 적용된 deployment에는 반영되지 않음
   - 파드가 볼륨 없이 실행되어 `/app/skills` 디렉토리가 없었음

2. **PVC는 정상적으로 생성되어 있었음**
   - PVC는 `Bound` 상태로 정상 작동 중

### 적용된 해결책

1. **Deployment 재적용:**
   ```bash
   kubectl apply -f k8s/deployment.yaml
   kubectl rollout status deployment/claude-skills -n dev
   ```

2. **볼륨 마운트 확인:**
   ```bash
   # 파드의 볼륨 마운트 확인
   kubectl describe pod <pod-name> -n dev | grep -A 5 "Mounts:"
   
   # 볼륨 디렉토리 확인
   kubectl exec <pod-name> -n dev -- ls -la /app/skills
   
   # 스킬 파일 확인
   kubectl exec <pod-name> -n dev -- find /app/skills -type f -name "SKILL.md"
   ```

3. **결과:**
   - ✅ 볼륨이 `/app/skills`에 정상 마운트됨
   - ✅ 기존 스킬 파일들이 유지됨
   - ✅ 파드 재시작 시에도 데이터가 유지됨

## 롤아웃 절차

1. **PVC 재생성 (필요시):**
   ```bash
   # 먼저 deployment 스케일 다운 (볼륨 사용 해제)
   kubectl scale deployment claude-skills --replicas=0 -n dev
   
   # 기존 PVC 삭제 (주의: 데이터 손실 가능)
   kubectl delete pvc claude-skills-storage -n dev
   
   # 새 PVC 생성
   kubectl apply -f k8s/pvc.yaml
   
   # PVC가 Bound 상태가 될 때까지 대기
   kubectl wait --for=condition=Bound pvc/claude-skills-storage -n dev --timeout=60s
   ```

2. **Deployment 업데이트:**
   ```bash
   kubectl apply -f k8s/deployment.yaml
   ```

3. **롤아웃 확인:**
   ```bash
   kubectl rollout status deployment/claude-skills -n dev
   ```

4. **볼륨 마운트 및 데이터 확인:**
   ```bash
   # 파드 이름 확인
   POD_NAME=$(kubectl get pods -n dev -l app=claude-skills -o jsonpath='{.items[0].metadata.name}')
   
   # 볼륨 마운트 확인
   kubectl describe pod $POD_NAME -n dev | grep -A 5 "Mounts:"
   
   # 볼륨 디렉토리 확인
   kubectl exec $POD_NAME -n dev -- ls -la /app/skills
   
   # 스킬 파일 확인
   kubectl exec $POD_NAME -n dev -- find /app/skills -type f -name "SKILL.md"
   ```

## 장기 해결 방안

### 1. Spot 인스턴스 대신 일반 노드 풀 사용

볼륨 안정성을 위해 Spot 인스턴스 대신 일반 노드 풀을 사용하는 것을 고려:

```yaml
# deployment.yaml
spec:
  template:
    spec:
      # nodeSelector 제거 또는 일반 노드로 변경
      # nodeSelector:
      #   cloud.google.com/gke-spot: "true"
```

### 2. ReadWriteMany 스토리지 사용

여러 노드에서 동시에 마운트 가능한 스토리지 사용 (NFS 등):

```yaml
# pvc.yaml
spec:
  storageClassName: nfs-client  # 또는 클러스터의 ReadWriteMany 스토리지 클래스
  accessModes:
    - ReadWriteMany
```

### 3. 백업 자동화

볼륨 데이터를 정기적으로 백업하는 CronJob 추가:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: skills-backup
  namespace: dev
spec:
  schedule: "0 2 * * *"  # 매일 새벽 2시
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: backup
            image: busybox
            command:
            - sh
            - -c
            - tar czf /backup/skills-$(date +%Y%m%d).tar.gz -C /app/skills .
            volumeMounts:
            - name: skills-storage
              mountPath: /app/skills
            - name: backup-storage
              mountPath: /backup
          volumes:
          - name: skills-storage
            persistentVolumeClaim:
              claimName: claude-skills-storage
          - name: backup-storage
            persistentVolumeClaim:
              claimName: skills-backup-storage
          restartPolicy: OnFailure
```

## 참고 자료

- [Kubernetes Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
- [GKE Storage Classes](https://cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/gce-pd-csi-driver)
- [ReadWriteOnce vs ReadWriteMany](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes)
