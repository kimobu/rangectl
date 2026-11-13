# Activity Simulation

`rangectl activity` generates background endpoint behavior from personas and
profiles in a range definition. It compiles one agent configuration per host;
Ansible installs the agents; and collection commands retrieve their JSONL logs.
Actions can model routine browsing, mail processing, application use, file
creation, and scheduled scenario events.

## Normal workflow

Validate personas:

```bash
./rangectl activity validate example
```

Compile per-host agent configs:

```bash
./rangectl activity compile example
```

`./rangectl render example` also compiles activity configs when
`activity.enabled` is true.

Install and start the agent on endpoints:

```bash
./rangectl configure example --phase activity-simulation
```

Collect endpoint logs back to the control host:

```bash
./rangectl configure example --phase activity-collect
```

Print collected local logs:

```bash
./rangectl activity collect example
```

Print log paths instead of log contents:

```bash
./rangectl activity collect example --paths
```

## Local testing

Run one compiled agent config locally:

```bash
./rangectl activity run state/example/activity/user-workstation.activity.json
```

Run one cycle without opening apps or changing files:

```bash
./rangectl activity run state/example/activity/user-workstation.activity.json --once --dry-run
```

Agents write structured JSONL records to each host's activity state directory.

## Triggers

A trigger queues one action for a compiled agent, such as a phishing click,
form submission, or scenario-specific command.

```bash
./rangectl activity trigger example \
  --hosts user-workstation \
  --type email.phishing \
  --arg submit_url=http://www.example.test/login \
  --trackable-id phish-001
```

Repeat `--arg key=value` for multiple arguments. A value may contain JSON when
the action expects structured data.

## Range configuration

Activity configuration lives under `activity` in the range file.
`activity.enabled` must be `true` before `compile` writes agent configs.

Each persona maps one user to one host, role, and email backend:

```yaml
activity:
  enabled: true
  ai:
    provider: static
    cache_dir: content-cache
  defaults:
    loop: true
    max_actions_per_cycle: 3
    cycle_delay: 300
    cycle_jitter: 120
    action_delay_jitter: 15
  personas:
    - id: alex-user
      username: alex.user
      display_name: Alex User
      host: user-workstation
      role: sales
      email:
        address: alex.user@example.test
        backend: outlook
```

`cycle_jitter` randomizes the delay between agent cycles. `action_delay_jitter`
randomizes action delays unless an action sets its own delay jitter.

## Profiles

Profiles define the action mix for a role. If a range does not define profiles,
`rangectl` provides defaults for office, sales, IT, and HR users.

Example profile:

```yaml
activity:
  profiles:
    sales:
      actions:
        - type: browser.visit
          probability: 80
          delay_after: 30
          delay_after_jitter: 10
          args:
            urls:
              - https://google.com
              - https://youtube.com
              - http://www.example.test
            reuse_tab: true
            max_tabs: 4
        - type: email.process
          probability: 50
          args:
            mode: routine
        - type: application.open_close
          probability: 35
          args:
            close_probability: 70
            min_dwell_seconds: 8
            max_dwell_seconds: 45
        - type: office.word
          probability: 40
          args:
            topic: customer notes
```

Browser actions use a packaged website list when available. macOS browser
actions reuse Safari tabs by default and cap the tab count.

Default macOS profiles also open normal apps such as Calendar, Notes, Reminders,
Calculator, Preview, and System Settings. The agent waits briefly and usually
closes the app after the action.

`shell.run` and scenario command actions execute argument lists without a shell
by default. Set `args.shell: true` (or `shell: true` on a scenario command step)
only when the command intentionally needs pipes, redirects, or shell expansion.
Command output is retained only as a bounded tail in activity logs.

## Email backends

Use `backend: outlook` for an AD-domain Windows user who should interact through
the Outlook desktop client. The adapter runs as the logged-in user through
Outlook COM automation, which updates Outlook process activity, mailbox and
unread state, attachment saves, Sent Items, and link clicks.

Use `backend: external` for non-domain or internet-style mail. It uses IMAP and
SMTP. Set the standard environment values in `.rangectl.env` unless the persona
uses custom variable names:

```bash
ACTIVITY_IMAP_HOST=mail.example.test
ACTIVITY_IMAP_PORT=993
ACTIVITY_IMAP_SSL=true
ACTIVITY_SMTP_HOST=mail.example.test
ACTIVITY_SMTP_PORT=587
ACTIVITY_SMTP_SSL=starttls
ACTIVITY_MAIL_USERNAME=bot@example.test
ACTIVITY_MAIL_PASSWORD=secret
```

Persona-specific environment names:

```yaml
email:
  address: alex.user@example.test
  backend: external
  settings:
    imap_host_env: ALEX_IMAP_HOST
    smtp_host_env: ALEX_SMTP_HOST
    username_env: ALEX_MAIL_USER
    password_env: ALEX_MAIL_PASSWORD
```

## AI content

The default `static` provider creates deterministic synthetic content with no
network access.

To use an OpenAI-compatible endpoint:

```yaml
activity:
  ai:
    provider: openai-compatible
    endpoint_env: OPENAI_BASE_URL
    api_key_env: OPENAI_API_KEY
    model_env: OPENAI_MODEL
    default_model: gpt-4.1-mini
```

Generated content is cached under each agent state directory by default. A
cached response can be reused during a class if the model endpoint is
unavailable.

## Agent state

The `activity_agent` Ansible role supports three states:

- `started`: install and start the agent.
- `stopped`: stop the running agent but leave files in place.
- `absent`: remove startup/service launchers and stop the agent.

By default, an agent keeps its logs and bookkeeping in its local activity state
directory. The authenticated state server can instead hold agent
configuration, action logs, dwell schedules, manifests, and scenario state.
This keeps scenario details and activity records off the endpoint.

Set these role variables to use remote configuration and state:

```yaml
activity_agent_remote_config_url: >-
  http://state-server.example.test:8766/v1/state/example/user-workstation/profile.json
activity_agent_scenario_state_url: http://state-server.example.test:8766
activity_agent_scenario_state_token: endpoint-scoped-token
```

The launcher maps those values to `RANGECTL_ACTIVITY_CONFIG_URL`,
`RANGECTL_ACTIVITY_CONFIG_TOKEN`, `RANGECTL_SCENARIO_STATE_URL`, and
`RANGECTL_SCENARIO_STATE_TOKEN`. An endpoint-scoped token can access only the
range and host used to derive it. The server's master token retains
administrative access to all documents.

When the agent first connects successfully, it uploads legacy local scenario
state and activity logs before removing those endpoint copies. Large state
documents are compressed in transit. Action execution continues if remote log
delivery fails; the agent does not write a local fallback log in remote mode.

The bundled server accepts bearer tokens over HTTP. Bind it only to an isolated
lab network, restrict the port to activity-agent hosts, or place it behind a TLS
reverse proxy. Use a unique master token of at least 32 characters and deliver
only scoped tokens to endpoints.
