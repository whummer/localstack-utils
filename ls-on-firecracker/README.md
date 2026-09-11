# LocalStack on Firecracker

A demo that boots [LocalStack](https://localstack.cloud) inside a
[Firecracker](https://firecracker-microvm.github.io/) microVM — the same
technology AWS Lambda itself runs on — and exercises it over the network with
an S3 bucket and a real Lambda deploy + invoke.

## How it works

1. **`make download`** grabs the `firecracker` binary, LocalStack's own
   [`lstk`](https://docs.localstack.cloud/aws/developer-tools/running-localstack/lstk/)
   CLI, a guest kernel and a base Ubuntu rootfs from Firecracker's public CI
   artifacts.
2. **`make rootfs`** clones the base rootfs, grows it, and chroots in to
   install Docker and drop in the `lstk` binary, then registers `systemd`
   units so Docker and then `lstk start` come up on boot. Building happens on
   the host via a loop-mounted image, so the customization step itself
   doesn't need the guest to be running.
3. **`make up`** creates a tap network device on the host, NATs the guest out
   through the host's default interface (`lstk` needs to pull the LocalStack
   image, and LocalStack itself needs to pull the Lambda runtime image at
   invoke time), and boots the image with Firecracker. It polls
   `http://<vm-ip>:4566/_localstack/health` until LocalStack is ready.
4. **`make test`** creates an S3 bucket and round-trips an object, then
   deploys a small Python Lambda function and invokes it. The function
   itself creates a *second* bucket and lists all buckets via `boto3`
   (LocalStack injects `AWS_ENDPOINT_URL` into the Lambda execution
   environment automatically, so no endpoint code is needed) — proving the
   Lambda's own AWS calls land on the same LocalStack backend as the CLI
   calls above: it sees the first bucket, and the one it creates is visible
   back on the CLI afterward. `lstk` runs LocalStack as a container against
   the guest's own Docker daemon, which is also what LocalStack itself uses
   to spawn the Lambda executor container — the same two-layer shape real
   Lambda uses (a container runtime inside a Firecracker microVM).
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
- A `LOCALSTACK_AUTH_TOKEN` environment variable on the host, set to a
  LocalStack **CI Auth Token** (not a personal Developer Auth Token — `lstk`
  rejects those non-interactively). It's passed through into the guest's
  `localstack.service` at boot. Get one from
  [your LocalStack workspace](https://app.localstack.cloud/workspace/auth-tokens).

## What's actually running

Docker runs *inside* the guest OS (installed at rootfs-build time), and
`lstk` uses it to pull and run the LocalStack container, which in turn uses
the same Docker daemon as its normal Docker-based Lambda executor. The guest
needs internet access both to pull the LocalStack image and, at Lambda
invoke time, the runtime image — which is why `run-vm.sh` sets up NAT
through the host rather than an isolated host-only network.

## Caveats

The base rootfs comes from Firecracker's own **CI test artifacts** — it's
what their integration tests boot, not a general-purpose image, and it's
stripped down accordingly (no `/var/cache/apt`, `/var/log`, or populated
dpkg database out of the box; `build-rootfs.sh` reconstructs what apt/Docker
need). It works, but a more idiomatic base for "run a Docker image as a
Firecracker rootfs" is `docker export`-ing a real image (e.g. `ubuntu:22.04`)
onto a formatted ext4 device. For running actual container workloads inside
Firecracker in production, see
[firecracker-containerd](https://github.com/firecracker-microvm/firecracker-containerd)
(what AWS Lambda/Fargate use) instead of a full Docker-in-VM setup like this
one.

Relatedly: the kernel doesn't have `CONFIG_IP_NF_RAW` (and can't load it —
module loading is compiled out entirely), which Docker 28+ needs for a
hardening rule that stops a container from being reached directly,
bypassing its published-port restriction. `build-rootfs.sh` sets
`DOCKER_INSECURE_NO_IPTABLES_RAW=1` (Docker's documented opt-out for exactly
this case) to work around it. The tradeoff — a container published to
`127.0.0.1` becomes reachable from other hosts on the same network — is
acceptable for this single-tenant, ephemeral microVM reachable only over its
own host-only tap network, but wouldn't be on a shared or long-lived host.

## Layout

```
Makefile               self-describing entry point (make help)
scripts/
  download-assets.sh   fetch firecracker + lstk + kernel + base rootfs
  build-rootfs.sh       install Docker + lstk into a working rootfs image
  run-vm.sh             set up networking (incl. NAT) and boot the microVM
  smoke-test.sh         S3 round-trip + Lambda deploy/invoke against it
  teardown.sh           stop the VM and remove the tap device / NAT rules
fixtures/
  handler.py            the demo Lambda function
work/                   downloaded/generated artifacts (git-ignored)
```
