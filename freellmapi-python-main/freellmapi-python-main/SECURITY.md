# 🔒 Security Policy

## 🛡️ Security Overview

**FreeLLMAPI Python** is designed with a **privacy-first, local-first architecture**. 

- **Local Execution**: All routing, credential management, and rate-limit tracking happen 100% on your local machine or self-hosted server.
- **AES-256-GCM Encryption**: All upstream provider API keys stored in SQLite are encrypted at rest with AES-256-GCM using unique per-key Initialization Vectors (IV) and 128-bit authentication tags.
- **Zero Telemetry / Key Sharing**: Your upstream API keys are never forwarded to third parties other than the respective AI providers you have explicitly configured.
- **Local Dashboard Authentication**: The management dashboard is protected via PBKDF2-HMAC-SHA256 salted password hashing and secure token-based session verification.

---

## 🚨 Reporting a Vulnerability

If you discover a security vulnerability in FreeLLMAPI Python, please **do NOT report it via public GitHub issues**.

Instead, please report it via private GitHub Security Advisory or directly contact the maintainers.

Please include:
1. Description of the vulnerability.
2. Steps to reproduce or proof-of-concept.
3. Potential impact.

We will review and respond to valid security disclosures promptly. Thank you for helping keep our users safe!
