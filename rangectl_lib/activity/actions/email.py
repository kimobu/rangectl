from __future__ import annotations

import base64
import imaplib
import os
import platform
import random
import re
import smtplib
import ssl
import subprocess
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
from typing import Any, Callable


def register(add: Callable[[str, Any], None]) -> None:
    add("email.process", process)
    add("email.send", send)
    add("email.phishing", phishing)


def process(context: Any, action: Any) -> dict[str, Any]:
    backend = _backend(context, action)
    if backend == "outlook":
        return _outlook_process(context, action)
    return _external_process(context, action)


def send(context: Any, action: Any) -> dict[str, Any]:
    backend = _backend(context, action)
    if backend == "outlook":
        return _outlook_send(context, action)
    return _external_send(context, action)


def phishing(context: Any, action: Any) -> dict[str, Any]:
    decision = _phishing_decision(context, action)
    if not decision["open"]:
        return {"decision": decision, "status": "ignored"}
    process_args = dict(action.args)
    process_args["click_links"] = bool(decision["click"])
    process_args["save_attachments"] = bool(decision["save_attachment"])
    process_action = type(action)(
        type="email.process",
        args=process_args,
        probability=100,
        delay_before=0,
        delay_after=0,
        trackable_id=action.trackable_id,
    )
    result = process(context, process_action)
    result["decision"] = decision
    if decision["submit"] and action.args.get("submit_url"):
        from . import browser

        submit_action = type(action)(
            type="browser.form_submit",
            args={
                "url": action.args["submit_url"],
                "fields": action.args.get("fields") or {"username": "{email}", "password": "{password}"},
                "open_first": True,
            },
            probability=100,
            delay_before=0,
            delay_after=0,
            trackable_id=action.trackable_id,
        )
        result["submission"] = browser.form_submit(context, submit_action)
    return result


def _backend(context: Any, action: Any) -> str:
    return str(action.args.get("backend") or context.config.persona.email.backend or "external").lower()


def _phishing_decision(context: Any, action: Any) -> dict[str, bool | int]:
    traits = context.config.persona.traits
    susceptibility = int(action.args.get("susceptibility", traits.get("phishing_susceptibility", 35)))
    open_threshold = _clamp(susceptibility + int(action.args.get("open_bias", 20)))
    click_threshold = _clamp(susceptibility + int(action.args.get("click_bias", 0)))
    submit_threshold = _clamp(susceptibility + int(action.args.get("submit_bias", -20)))
    attachment_threshold = _clamp(susceptibility + int(action.args.get("attachment_bias", -10)))
    will_open = random.randint(1, 100) <= open_threshold
    will_click = will_open and random.randint(1, 100) <= click_threshold
    return {
        "score": susceptibility,
        "open": will_open,
        "click": will_click,
        "submit": will_click and random.randint(1, 100) <= submit_threshold,
        "save_attachment": will_open and random.randint(1, 100) <= attachment_threshold,
    }


def _clamp(value: int) -> int:
    return max(0, min(100, value))


def _settings(context: Any, action: Any) -> dict[str, Any]:
    settings = dict(context.config.persona.email.settings)
    settings.update(action.args.get("settings") or {})
    return settings


def _external_process(context: Any, action: Any) -> dict[str, Any]:
    settings = _settings(context, action)
    imap_host = _value(settings, "imap_host", "ACTIVITY_IMAP_HOST")
    username = _value(settings, "username", "ACTIVITY_MAIL_USERNAME") or context.config.persona.email.address
    password = _value(settings, "password", "ACTIVITY_MAIL_PASSWORD")
    if not imap_host or not username or not password:
        return {"backend": "external", "status": "skipped", "reason": "missing IMAP settings"}
    if context.dry_run:
        return {"backend": "external", "imap_host": imap_host, "username": username}

    imap_ssl = str(settings.get("imap_ssl", os.environ.get("ACTIVITY_IMAP_SSL", "true"))).lower() in {"true", "tls", "ssl", "1", "yes"}
    port = int(settings.get("imap_port", os.environ.get("ACTIVITY_IMAP_PORT", 993 if imap_ssl else 143)))
    mailbox = str(settings.get("mailbox", "INBOX"))
    imap_client = imaplib.IMAP4_SSL if imap_ssl else imaplib.IMAP4
    with imap_client(imap_host, port) as client:
        client.login(username, password)
        client.select(mailbox)
        _typ, data = client.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        if not ids:
            return {"backend": "external", "mailbox": mailbox, "unread": 0}
        message_id = ids[-1]
        _typ, fetched = client.fetch(message_id, "(RFC822)")
        raw = fetched[0][1]
        message = BytesParser(policy=default).parsebytes(raw)
        subject = str(message.get("subject", ""))
        if bool(action.args.get("mark_seen", True)):
            client.store(message_id, "+FLAGS", "\\Seen")
        return {"backend": "external", "mailbox": mailbox, "unread": len(ids), "subject": subject}


def _external_send(context: Any, action: Any) -> dict[str, Any]:
    settings = _settings(context, action)
    smtp_host = _value(settings, "smtp_host", "ACTIVITY_SMTP_HOST")
    username = _value(settings, "username", "ACTIVITY_MAIL_USERNAME") or context.config.persona.email.address
    password = _value(settings, "password", "ACTIVITY_MAIL_PASSWORD")
    if not smtp_host:
        return {"backend": "external", "status": "skipped", "reason": "missing SMTP host"}

    recipients = _recipients(context, action)
    subject = str(action.args.get("subject") or f"Follow up from {context.config.persona.display_name or context.config.persona.username}")
    body = str(action.args.get("body") or context.content.generate(
        persona=context.config.persona,
        topic=str(action.args.get("topic", "work follow-up")),
        kind="email",
        seed=subject,
    ))
    if context.dry_run:
        return {"backend": "external", "smtp_host": smtp_host, "to": recipients, "subject": subject}

    message = EmailMessage()
    message["From"] = context.config.persona.email.address or username
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    port = int(settings.get("smtp_port", os.environ.get("ACTIVITY_SMTP_PORT", 587)))
    use_ssl = str(settings.get("smtp_ssl", os.environ.get("ACTIVITY_SMTP_SSL", "starttls"))).lower()
    if use_ssl == "true":
        with smtplib.SMTP_SSL(smtp_host, port, context=ssl.create_default_context()) as client:
            _smtp_login(client, username, password)
            client.send_message(message)
    else:
        with smtplib.SMTP(smtp_host, port) as client:
            if use_ssl in {"starttls", "tls", "1"}:
                client.starttls(context=ssl.create_default_context())
            _smtp_login(client, username, password)
            client.send_message(message)
    return {"backend": "external", "to": recipients, "subject": subject}


def _outlook_process(context: Any, action: Any) -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return {"backend": "outlook", "status": "skipped", "reason": "Outlook automation requires Windows"}
    save_attachments = bool(action.args.get("save_attachments", False))
    click_links = bool(action.args.get("click_links", False))
    output_dir = str(action.args.get("output_directory", "~/Downloads"))
    script = rf"""
$ErrorActionPreference = "Stop"
$outlook = New-Object -ComObject Outlook.Application
$ns = $outlook.GetNamespace("MAPI")
$inbox = $ns.GetDefaultFolder(6)
$items = $inbox.Items
$items.Sort("[ReceivedTime]", $true)
$unread = 0
$subject = ""
foreach ($item in $items) {{
  if ($item.UnRead -eq $true) {{
    $unread += 1
    if ($subject -eq "") {{
      $subject = [string]$item.Subject
      $item.UnRead = $false
      if ({_ps_bool(save_attachments)}) {{
        $dir = [Environment]::ExpandEnvironmentVariables("{output_dir}")
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        foreach ($attachment in $item.Attachments) {{
          $attachment.SaveAsFile((Join-Path $dir $attachment.FileName))
        }}
      }}
      if ({_ps_bool(click_links)}) {{
        $body = [string]$item.HTMLBody + "`n" + [string]$item.Body
        $match = [regex]::Match($body, "https?://[^\s<`"']+")
        if ($match.Success) {{ Start-Process $match.Value }}
      }}
    }}
  }}
}}
[pscustomobject]@{{ backend="outlook"; unread=$unread; subject=$subject }} | ConvertTo-Json -Compress
"""
    if context.dry_run:
        return {"backend": "outlook", "click_links": click_links, "save_attachments": save_attachments}
    return _run_powershell_json(script)


def _outlook_send(context: Any, action: Any) -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return {"backend": "outlook", "status": "skipped", "reason": "Outlook automation requires Windows"}
    recipients = _recipients(context, action)
    subject = str(action.args.get("subject") or f"Follow up from {context.config.persona.display_name or context.config.persona.username}")
    body = str(action.args.get("body") or context.content.generate(
        persona=context.config.persona,
        topic=str(action.args.get("topic", "work follow-up")),
        kind="email",
        seed=subject,
    ))
    script = rf"""
$ErrorActionPreference = "Stop"
$outlook = New-Object -ComObject Outlook.Application
$mail = $outlook.CreateItem(0)
$mail.To = "{';'.join(_ps_escape(item) for item in recipients)}"
$mail.Subject = "{_ps_escape(subject)}"
$mail.Body = "{_ps_escape(body)}"
$mail.Send()
[pscustomobject]@{{ backend="outlook"; to="{';'.join(_ps_escape(item) for item in recipients)}"; subject="{_ps_escape(subject)}" }} | ConvertTo-Json -Compress
"""
    if context.dry_run:
        return {"backend": "outlook", "to": recipients, "subject": subject}
    return _run_powershell_json(script)


def _run_powershell_json(script: str) -> dict[str, Any]:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    output = completed.stdout.strip()
    if not output:
        return {"backend": "outlook"}
    import json

    return json.loads(output.splitlines()[-1])


def _recipients(context: Any, action: Any) -> list[str]:
    recipients = action.args.get("to") or action.args.get("recipients")
    if isinstance(recipients, str):
        recipients = [recipients]
    if recipients:
        return [str(item) for item in recipients]
    domain = context.config.persona.email.address.rsplit("@", 1)[-1] if "@" in context.config.persona.email.address else "example.local"
    return [f"it-helpdesk@{domain}"]


def _value(settings: dict[str, Any], key: str, env_name: str) -> str:
    if settings.get(key):
        return str(settings[key])
    if settings.get(f"{key}_env"):
        return os.environ.get(str(settings[f"{key}_env"]), "")
    return os.environ.get(env_name, "")


def _smtp_login(client: smtplib.SMTP, username: str, password: str) -> None:
    if username and password:
        client.login(username, password)


def _ps_escape(value: str) -> str:
    return re.sub(r'(["`$])', r"`\1", value).replace("\r", "").replace("\n", "`r`n")


def _ps_bool(value: bool) -> str:
    return "$true" if value else "$false"
