# ytt-4rl: Confirm ardenone-cluster Longhorn StorageClass

## Task
Confirm Longhorn StorageClass name for the ytt PVC manifest. Blocks Phase 4 manifest / Phase 10.

## Finding
**StorageClass: `longhorn`**

### Cluster Query Results
```bash
kubectl --server=http://traefik-ardenone-cluster:8001 get storageclass
```

| Name | Provisioner | ReclaimPolicy | VolumeBindingMode | AllowVolumeExpansion | Default |
|------|-------------|---------------|-------------------|----------------------|---------|
| longhorn | driver.longhorn.io | Delete | Immediate | true | ✓ (default) |
| longhorn-ha | driver.longhorn.io | Delete | Immediate | true | |
| longhorn-static | driver.longhorn.io | Delete | Immediate | true | |

### Manifest Status
The PVC manifest at `deploy/k8s/ardenone-cluster/ytt/pvc.yaml` already correctly specifies:
```yaml
storageClassName: longhorn
```

### Conclusion
- StorageClass name is **`longhorn`**
- Default StorageClass on ardenone-cluster
- Manifest is already correct
- No changes needed — this task was a verification spike

## Date
2025-01-17
