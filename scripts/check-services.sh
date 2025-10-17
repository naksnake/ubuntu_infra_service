
#!/usr/bin/env bash
set -euo pipefail

docker ps --format 'table {{.Names}}	{{.Status}}	{{.Ports}}'
ss -ulnp | awk 'NR==1 || /:(67|68|69)/'
ss -tlnp | awk 'NR==1 || /:(8080|8081|50000)/'
docker logs --tail 50 sit_dhcp || true
ls -l services/tftp/tftpboot
ls -l data/webfs_share
