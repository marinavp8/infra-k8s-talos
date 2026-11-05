# Talos Linux + Kubernetes con Ansible y Helm (QEMU/HVF en Apple Silicon)

Cluster Talos Linux (1 control plane + 2 workers) sobre VMs QEMU, desplegado íntegramente con Ansible.
Los servicios de Kubernetes se instalan con Helm desde Ansible (`kubernetes.core`).

> **Nota:** el objetivo inicial era VirtualBox. En Apple Silicon no es viable (ver [Problemas](#problemas-encontrados-y-soluciones)), así que se usa QEMU con aceleración HVF.

## Requisitos

```bash
brew install qemu zstd helm kubectl
pip install kubernetes
ansible-galaxy collection install -r requirements.yml   # kubernetes.core >= 6 (Helm 4)
```

Probado con: macOS arm64, Ansible core 2.17, QEMU 11.1, Helm 4.0, Talos v1.14.5 (Kubernetes v1.37.1).

## Uso

```bash
ansible-playbook site.yml                  # VMs + Talos + servicios (todo, ~8 min)
ansible-playbook site.yml --tags vms       # solo VMs QEMU
ansible-playbook site.yml --tags talos     # config, bootstrap, kubeconfig
ansible-playbook site.yml --tags services  # Helm: metrics-server, cert-manager, Traefik, whoami
ansible-playbook destroy.yml               # borra VMs, discos, secretos y kubeconfig

export KUBECONFIG=$PWD/.state/kubeconfig
export TALOSCONFIG=$PWD/.state/talos/talosconfig     # binario: .state/bin/talosctl
kubectl get nodes
curl http://whoami.localhost:8080
```

Los playbooks son idempotentes: se pueden relanzar sobre un cluster existente.

## Estructura

```
ansible.cfg, requirements.yml, inventory/hosts.yml
group_vars/all.yml            # versiones, nodos, puertos, servicios activables
site.yml                      # 3 plays: vms -> talos -> services
destroy.yml
scripts/l2hub.py              # hub L2 UDP entre VMs
roles/qemu_vms/               # imagen Talos -> qcow2, UEFI, arranque QEMU, hub
roles/talos/                  # talosctl, gen config, parches por nodo, apply, bootstrap, kubeconfig
roles/k8s_services/           # Helm + manifiestos (ClusterIssuer, whoami)
.state/                       # (gitignored) binarios, imágenes, discos, secretos de Talos, kubeconfig
```

## Arquitectura de red (sin root)

| NIC | Tipo QEMU | Uso |
|-----|-----------|-----|
| 1 | `user` (slirp) | Salida a Internet (pull de imágenes) y port-forward host→VM |
| 2 | `dgram` → hub UDP | L2 entre nodos, red fija `10.5.0.0/24` |

Nodos: `cp1 10.5.0.11`, `w1 10.5.0.21`, `w2 10.5.0.22`.

Port-forwards en `127.0.0.1`: Talos API `50001/50002/50003` (cp1/w1/w2), API de Kubernetes `6443`, Traefik `8080→30080` y `8443→30443` (vía talos-w1).
`talosctl` habla con los nodos a través del endpoint `127.0.0.1:50001` (el control plane hace de proxy hacia los workers por la red `10.5.0.x`).

Servicios (`group_vars/all.yml → services`): `metrics_server`, `cert_manager`, `traefik`, `whoami`. Traefik va por NodePort; no hay MetalLB (sus IPs no serían alcanzables desde el host).

## Problemas encontrados y soluciones

### 1. VirtualBox en Apple Silicon: Talos se bloquea al arrancar
Cadena de problemas, en orden:
- **`vboxvga` falla** al arrancar (`VERR_PGM_RAM_CONFLICT`) → usar `--graphicscontroller qemuramfb` o `vmsvga`.
- **SATA y NVMe no los ve el firmware ARM** (UEFI Shell: `map: No mapping found`). La controladora que funciona es `--add virtio --controller VirtIO` (no `scsi`/`VirtioSCSI`).
- **El firmware cae al shell UEFI** en vez de arrancar: hay que mover la entrada `UEFI VBOX HARDDISK` a la primera posición con `bcfg boot mv`. Con la ISO no arrancaba solo.
- **Bloqueo definitivo:** Talos se queda en `crypto/rand: blocked for 60 seconds waiting to read random data from the kernel`. El log de VirtualBox muestra `FEAT_RNG = 0` (la CPU virtual no tiene RNDR) y VirtualBox no tiene dispositivo virtio-rng → no hay entropía y Talos no levanta la red.
- **Decisión:** cambiar a QEMU con `-device virtio-rng-pci`. Talos arranca en ~3 s.

### 2. Red entre VMs en QEMU sin root
- `-netdev socket,mcast=...` **no reenvía tramas entre procesos en macOS**, ni por `127.0.0.1` ni por la IP de `en0`. Síntoma: `no route to host` hacia `10.5.0.11` (el ARP nunca resuelve) y el worker no puede firmar su CSR (`failed to sign API server CSR`), así que apid no arranca.
- **Solución:** `scripts/l2hub.py`, un hub UDP que reenvía cada trama al resto de VMs; las VMs usan `-netdev dgram`. Ansible lo arranca con `nohup` y guarda el pid en `.state/vms/hub.pid`.
- Alternativas descartadas: `socket_vmnet` / `vmnet-shared` (requieren root).

### 3. Sintaxis de `talosctl`
- `--insecure` es flag del subcomando: `talosctl get disks --insecure -n 127.0.0.1:50001` (no `talosctl --insecure get ...`).
- En modo mantenimiento basta `-n host:puerto`; no hace falta `-e`.

### 4. Talos 1.14 usa documentos tipados
`apply-config` falló con `static hostname is already set`, `.machine.kubelet.nodeIP is already set`, `kubelet config is already set`. La config generada ya incluye `HostnameConfig`, `KubeNodeConfig`, `KubeletConfig`, etc., que chocan con los campos legacy.
- Se eliminó `machine.network.hostname` (queda el hostname automático `talos-xxx-xxx`).
- `nodeIP.validSubnets` se mueve a un documento `KubeNodeConfig`.
- Consecuencia: los nombres de nodo en Kubernetes no coinciden con `talos-cp1/w1/w2` (esos son los nombres de VM).

### 5. Flannel usa la NIC equivocada
Con dos NIC, Flannel eligió la de la ruta por defecto (slirp, `10.0.2.15`, **la misma IP en todos los nodos**), así que el VXLAN no funcionaba entre nodos. Síntomas: webhook de cert-manager con timeout desde el apiserver, `metrics.k8s.io` no disponible, `startupapicheck` en Error.
- **Solución:** `--iface-regex=10\.5\.0\..*` en flanneld (documento `KubeFlannelCNIConfig` + parche idempotente del DaemonSet en el rol `talos`, porque Talos solo regenera los manifiestos en bootstrap).

### 6. NodePort inalcanzable desde el host
El port-forward de slirp entra por `10.0.2.15`, pero kube-proxy solo atiende NodePorts en la IP primaria del nodo → `Connection reset by peer` en `localhost:8080`.
- **Solución:** documento `KubeProxyConfig` con `nodePortAddresses: [0.0.0.0/0]`.
- Límite conocido: los cambios de `KubeProxyConfig` y de `KubeFlannelCNIConfig` aplicados a un cluster ya creado no regeneran los manifiestos; en cluster nuevo funcionan directamente.

### 7. Ansible / Helm
- `kubernetes.core 3.2.0` **no es compatible con Helm 4** (`unknown flag: --all` en `helm list`) → `kubernetes.core >= 6`. Ojo: si hay dos versiones instaladas, gana la de `~/.ansible/collections`.
- `playbook_dir` apunta a `/dev` si se pasa el playbook por stdin → usar siempre `site.yml`.
- Los `ansible-playbook ... | tail` lanzados en segundo plano devolvían `exit code 0` aunque el playbook fallara (es el código de `tail`). Redirigir a un log y comprobar `PLAY RECAP`.
- `sed -i` en macOS requiere `sed -i ''`.

### 8. QEMU / otros detalles
- `qemu-img` del PATH era el del Android SDK (v2.12); se usa la ruta absoluta de Homebrew.
- No hay `edk2-aarch64-vars.fd` en Homebrew: se crea un fichero de 64 MB vacío (`truncate -s 64m`) como NVRAM por VM.
- La imagen de Talos es `metal-arm64.raw.zst` (no `.xz`) → requiere `zstd` local.
- Con la imagen de disco (no ISO) no hace falta tocar el orden de arranque UEFI en QEMU: arranca solo.

## Limitaciones

- Todo corre en el Mac: el hub L2 es un proceso Python en segundo plano; si se mata, los nodos pierden la red entre sí (se relanza con `--tags vms`).
- Las VMs no sobreviven a un reinicio del Mac sin volver a ejecutar `site.yml --tags vms`.
- Los endpoints solo son accesibles desde `localhost`. Para exponer servicios en una IP real haría falta `socket_vmnet`/`vmnet` (root) y MetalLB.
- Secretos de Talos en `.state/talos/` (no subir a git).
