# Security Policy

## Disclaimer

This is a community-maintained open-source project and is **not affiliated with, endorsed by, or sponsored by VMware, Inc. or Broadcom Inc.** "VMware" and "Aria" are trademarks of Broadcom Inc.

**Author**: Wei Zhou, VMware by Broadcom — wei-wz.zhou@broadcom.com

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it privately:

- **Email**: wei-wz.zhou@broadcom.com
- **GitHub**: Open a [private security advisory](https://github.com/vmware-skills/VMware-Aria/security/advisories/new)

Do **not** open a public GitHub issue for security vulnerabilities.

## Security Design

### Credential Management

- Passwords are stored exclusively in `~/.vmware-aria/.env` (never in `config.yaml`, never in code)
- `.env` file permissions are verified at startup (`chmod 600` required)
- No credentials are logged, echoed, or included in audit entries
- Each Aria Operations target uses a separate environment variable: `VMWARE_ARIA_<TARGET_NAME_UPPER>_PASSWORD`

### Read-Heavy Design

21 of 27 tools are read-only. Write operations are limited to:
- Alert acknowledge and cancel
- Alert definition management
- Report generation and management

No VM lifecycle, no networking, no storage operations.

### Audit Logging

All operations (read and write) are logged to `~/.vmware/audit.db` (SQLite WAL) via the `@vmware_tool` decorator with timestamp, user, target, operation, parameters, and result.

### No Outbound Network Calls

This skill makes no webhook, HTTP listener, or outbound network connections beyond the user-configured Aria Operations REST API endpoint (HTTPS 443). No data is sent to third-party services.

### SSL/TLS Verification

- TLS certificate verification is **enabled by default**
- `verify_ssl: false` exists solely for environments using self-signed certificates
- In production, always use CA-signed certificates with full TLS verification

### Transitive Dependencies

- `vmware-policy` is the only transitive dependency auto-installed; it provides the `@vmware_tool` decorator and audit logging
- No post-install scripts or background services are started during installation

### Prompt Injection Protection

- All Aria-sourced content (alert descriptions, resource names, metric labels) is processed through `_sanitize()`
- Sanitization truncates to 500 characters and strips C0/C1 control characters

## Static Analysis

This project is scanned with [Bandit](https://bandit.readthedocs.io/) before every release, targeting 0 Medium+ issues:

```bash
uvx bandit -r vmware_aria/
```

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.5.x   | Yes       |
| < 1.5   | No        |
