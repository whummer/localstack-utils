# How it works

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

`make ssh` drops you into a root shell on the running microVM (the base
image ships a pre-authorized SSH key, fetched by `download-assets.sh`
alongside the kernel/rootfs). `make diagnose` dumps `systemctl status` and
`journalctl` for the `docker`/`localstack` services — the same thing the CI
workflow does automatically on failure.

## What's actually running

Docker runs *inside* the guest OS (installed at rootfs-build time), and
`lstk` uses it to pull and run the LocalStack container, which in turn uses
the same Docker daemon as its normal Docker-based Lambda executor. The guest
needs internet access both to pull the LocalStack image and, at Lambda
invoke time, the runtime image — which is why `run-vm.sh` sets up NAT
through the host rather than an isolated host-only network.

## Caveats

The base rootfs comes from Firecracker's own **CI test artifacts** — what
their integration tests boot, not a general-purpose image. It's stripped
down accordingly (no `/var/cache/apt`, `/var/log`, or populated dpkg
database out of the box; `build-rootfs.sh` reconstructs what apt/Docker
need), and its kernel is minimal too (no loadable modules, no `nf_tables`,
no `CONFIG_IP_NF_RAW`) — a few of the fixes in `build-rootfs.sh` exist
specifically to work around that (switching Docker to the legacy iptables
backend, and opting out of a Docker 28+ hardening rule that needs a table
this kernel doesn't have via `DOCKER_INSECURE_NO_IPTABLES_RAW=1`, which is
fine for a single-tenant, ephemeral microVM but not something to carry into
a shared or long-lived host).

A more idiomatic base for "run a Docker image as a Firecracker rootfs" would
be `docker export`-ing a real image (e.g. `ubuntu:22.04`) onto a formatted
ext4 device rather than patching Firecracker's CI artifact. For running
actual container workloads inside Firecracker in production, see
[firecracker-containerd](https://github.com/firecracker-microvm/firecracker-containerd)
(what AWS Lambda/Fargate use) instead of a full Docker-in-VM setup like this
one.
