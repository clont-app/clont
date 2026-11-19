# security

clont is read-only by design: every cloud, vmware and kubernetes call it makes is
a read, and the iam policy in [docs/iam.md](docs/iam.md) is the whole of what it
needs. a way to make it write, delete or leak credentials is a security bug.

## reporting

please don't open a public issue. use github's private report instead:
**security → report a vulnerability** on this repo. you'll get an answer within a
few days.

only the latest release gets fixes.
