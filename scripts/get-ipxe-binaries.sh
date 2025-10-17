
#!/usr/bin/env bash
set -euo pipefail
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
TFTP_DIR="$REPO_DIR/services/tftp/tftpboot"
mkdir -p "$TFTP_DIR"
curl -fsSL -o "$TFTP_DIR/undionly.kpxe" https://boot.ipxe.org/undionly.kpxe
curl -fsSL -o "$TFTP_DIR/ipxe.efi" https://boot.ipxe.org/ipxe.efi
ls -lh "$TFTP_DIR"
