# SMTP submitter role

This role installs one-shot systemd services that submit configured messages to
an SMTP server. It is intended for isolated lab traffic where a direct TCP SMTP
connection must be visible to network monitoring.

The role does not enable SMTP authentication or transport encryption. Do not
use it to send mail across the public internet or through a production mail
service.

Required variables:

```yaml
smtp_submitter_host: mail.example.test
smtp_submitter_port: 25
smtp_submitter_messages:
  - id: sample-message
    from: sender@example.test
    to: recipient@example.test
    subject: Training message
    body: This message was generated inside the lab.
```

The role installs `rangectl-smtp-sample-message.service` but does not start it.
The range's scenario phase or operator runbook should start the unit at the
intended time.
