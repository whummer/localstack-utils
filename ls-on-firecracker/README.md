# LocalStack on Firecracker

A demo that boots [LocalStack](https://localstack.cloud) inside a
[Firecracker](https://firecracker-microvm.github.io/) microVM — the same
technology AWS Lambda itself runs on — and exercises it over the network with
an S3 bucket and a Lambda function that itself talks back to S3.

## Quick start

```
make up      # download -> build a LocalStack-flavored rootfs -> boot the microVM
make test    # S3 round-trip + Lambda deploy/invoke through it
make down    # stop the VM
```

Run `make help` for the full target list, including `make ssh` and
`make diagnose` for poking around inside the running microVM.

## Prerequisites

- A **Linux host with KVM** (`/dev/kvm`) — doesn't run on macOS or most cloud
  VMs without nested virtualization. Bare metal, an EC2 `.metal` instance, or
  a GitHub Actions `ubuntu-latest` runner all work; see
  `.github/workflows/test-ls-on-firecracker.yml` for a working CI setup.
- `curl`, `iproute2`, `iptables`, `e2fsprogs`, `zip`, `jq`, `ssh`, and the
  AWS CLI on the host, plus `sudo` access.
- A `LOCALSTACK_AUTH_TOKEN` environment variable set to a LocalStack **CI
  Auth Token** — get one from
  [your LocalStack workspace](https://app.localstack.cloud/workspace/auth-tokens).

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
docs/
  NOTES.md              how it works under the hood, and known caveats
work/                   downloaded/generated artifacts (git-ignored)
```

See [docs/NOTES.md](docs/NOTES.md) for the details.
