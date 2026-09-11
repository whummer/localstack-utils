# LocalStack on Firecracker

A demo that boots [LocalStack](https://localstack.cloud) inside a
[Firecracker](https://firecracker-microvm.github.io/) microVM — the same
technology AWS Lambda itself runs on — and exercises it over the network with
an S3 bucket and a real Lambda deploy + invoke.

## How it works

1. **`make download`** grabs the `firecracker` binary, a guest kernel and a
   base Ubuntu rootfs from Firecracker's public CI artifacts.
2. **`make rootfs`** clones the base rootfs, grows it, and chroots in to
   install Docker plus `pip install localstack awscli-local`, and registers
   `systemd` units so Docker and then `localstack start --host` come up on
   boot. Building happens on the host via a loop-mounted image, so the
   customization step itself doesn't need the guest to be running.
3. **`make up`** creates a tap network device on the host, NATs the guest out
   through the host's default interface (LocalStack needs to `docker pull`
   the Lambda runtime image at invoke time), and boots the image with
   Firecracker. It polls `http://<vm-ip>:4566/_localstack/health` until
   LocalStack is ready.
4. **`make test`** creates an S3 bucket and round-trips an object, then
   deploys a small Python Lambda function, invokes it, and asserts the
   response — all against the LocalStack instance running inside the
   microVM. Lambda execution goes through the guest's own Docker daemon,
   exactly like it would against a normal `docker run localstack` setup.
5. **`make down`** / **`make clean`** tear the VM, NAT rules, and tap device
   down again.

```
make up      # download -> rootfs -> boot the microVM
make test    # exercise S3 + Lambda through it
make down    # stop the VM
```

Run `make help` for the full target list. `make ssh` drops you into a root
shell on the running microVM (the base image ships a pre-authorized SSH key,
fetched by `download-assets.sh` alongside the kernel/rootfs); `make diagnose`
dumps `systemctl status` and `journalctl` for the `docker`/`localstack`
services, which is what the CI workflow does automatically on failure.

## Prerequisites

- A **Linux host with KVM** (`/dev/kvm` present and accessible) — Firecracker
  does not run on macOS or in most cloud VMs without nested virtualization.
  Bare metal, an EC2 `.metal` instance, or a GitHub Actions `ubuntu-latest`
  runner (which has KVM enabled) all work. See
  `.github/workflows/test-ls-on-firecracker.yml` in the repo root for a
  working CI setup.
- `curl`, `iproute2`, `iptables`, `e2fsprogs`, `zip`, `jq`, `ssh`, and the
  AWS CLI (`aws`) on the host.
- `sudo` access — the scripts use it for loop-mounting the rootfs image,
  managing the tap device and NAT rules, and launching `firecracker` itself.
- Optionally, a `LOCALSTACK_AUTH_TOKEN` environment variable on the host —
  if set, it's passed through into the guest's `localstack.service` at boot.

## What's actually running

Docker runs *inside* the guest OS (installed at rootfs-build time), and
LocalStack uses it as its normal Docker-based Lambda executor. The guest
needs internet access at Lambda invoke time to pull the runtime image, which
is why `run-vm.sh` sets up NAT through the host rather than an isolated
host-only network.

## Layout

```
Makefile               self-describing entry point (make help)
scripts/
  download-assets.sh   fetch firecracker + kernel + base rootfs
  build-rootfs.sh       install Docker + LocalStack into a working rootfs image
  run-vm.sh             set up networking (incl. NAT) and boot the microVM
  smoke-test.sh         S3 round-trip + Lambda deploy/invoke against it
  teardown.sh           stop the VM and remove the tap device / NAT rules
fixtures/
  handler.py            the demo Lambda function
work/                   downloaded/generated artifacts (git-ignored)
```
