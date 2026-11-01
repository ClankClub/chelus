"""Human-readable site copy. Setup stays aligned with the repository README."""
from html import escape


def code(text: str, language: str = 'sh') -> str:
    return f'<div class="code-block"><span class="code-language">{language}</span><button type="button" class="copy-button" aria-label="Copy code" data-copy hidden><img src="{{{{base}}}}assets/icons/copy.svg" alt="" width="18" height="18"></button><pre><code>{escape(text.strip())}</code></pre><span class="copy-status" role="status" aria-live="polite"></span></div>'


def page(title, heading, intro, body, kicker='THE CHELUS GUIDE'):
    return dict(title=title, description=intro, heading=heading, intro=intro, body=body, kicker=kicker)


PAGES = {
    'start': page('Get started', 'A small beginning.', 'From a fresh Termux session to your first conversation. No pip install required.', r'''
<h2 id="requirements">What you’ll need</h2>
<ul><li><a href="https://termux.dev/">Termux</a> and Python 3.11 or newer.</li><li>Git, to download the source.</li><li>A DeepSeek API key and an internet connection for inference. API usage is billed by your provider.</li></ul>
<p>The demonstrated device is a Samsung Galaxy A10e running Android 11 and 32-bit Termux. Other environments are a portability goal, not a tested support matrix. Node.js is not required.</p>
<h2 id="install">Install in Termux</h2>
<p>If Python and Git aren’t installed yet, add them through Termux’s package manager:</p>
''' + code('pkg update\npkg install python git') + r'''
<p>Then clone Chelus and put its launcher on your path:</p>
''' + code('git clone https://github.com/ClankClub/chelus.git ~/chelus\nln -sf ~/chelus/chelus.sh "$PREFIX/bin/chelus"') + r'''
<h2 id="api-key">Add your API key</h2>
<p>In your Termux shell, store your DeepSeek key in a file readable only by you. The <code>read</code> command waits for your paste; press Enter afterward. Your input stays hidden.</p>
''' + code('mkdir -p ~/.config/chelus && chmod 700 ~/.config/chelus\nread -rs DK\n(umask 077; printf \'%s\' "$DK" > ~/.config/chelus/deepseek.key); unset DK') + r'''
<p>Alternatively, set <code>DEEPSEEK_API_KEY</code> in the environment. It takes precedence over the saved key file. Never put a key in a project file, chat message, or Git commit.</p>
<h2 id="first-conversation">Start a conversation</h2>
''' + code('chelus') + r'''
<p>Try: “Remember that I keep my projects in ~/projects.” Start a new conversation with <code>/new</code> and ask where your projects live. Owner memory carries across conversations.</p>
<p>For a single reply from the terminal:</p>
''' + code('chelus ask "What can you help me do on this phone?"') + r'''
<p>For the battery tool, you’ll also need the Termux <code>termux-api</code> package and the Termux:API Android app. They’re optional for normal chat.</p>
<h2 id="next">Make it useful</h2>
<div class="next-links"><a href="{{base}}guide/telegram/">Connect Telegram <span>Talk to your on-phone agent from a private chat.</span></a><a href="{{base}}guide/memory-and-skills/">Teach it your workflow <span>Keep context and turn procedures into readable skills.</span></a><a href="{{base}}guide/tools-and-approvals/">Understand approvals <span>See what Chelus can run, and what requires your say-so.</span></a></div>
<h2 id="troubleshooting">If something gets stuck</h2>
<p>Check <code>python --version</code> first. If <code>chelus</code> is not found, rerun the launcher command above. Configuration errors exit with code <code>2</code>; model/API failures exit with code <code>1</code>. Verify your key and network connection without printing your key.</p>
<p>If DeepSeek is unavailable, Chelus retries with a countdown. Ctrl-C cancels in the terminal; <code>/stop</code> cancels Telegram retries. Read the <a href="{{base}}reference/#limits">current limits</a> before relying on unattended tasks.</p>
''', 'GET STARTED'),
    'guide': page('Guide', 'Make it your own.', 'Start with a conversation. Add the pieces that make Chelus useful to you.', r'''
<h2 id="first-steps">Choose your next step</h2>
<div class="guide-links"><a href="{{base}}guide/telegram/"><span class="eyebrow">01 / STAY IN TOUCH</span><h3>Telegram</h3><p>Set up your private bot, keep conversations separate, and understand the bridge.</p></a><a href="{{base}}guide/memory-and-skills/"><span class="eyebrow">02 / KEEP THE CONTEXT</span><h3>Memory &amp; skills</h3><p>Remember useful facts and save procedures you can inspect, revise, and undo.</p></a><a href="{{base}}guide/tools-and-approvals/"><span class="eyebrow">03 / DO THE WORK</span><h3>Tools &amp; approvals</h3><p>Search, edit, fetch, and run commands with an explicit approval boundary.</p></a><a href="{{base}}guide/scheduling/"><span class="eyebrow">04 / COME BACK TO IT</span><h3>Scheduled jobs</h3><p>Run prompts or scripts on a schedule while the Telegram bridge is running.</p></a></div>
<h2 id="mental-model">What runs where</h2>
<p>The agent loop, tools, local history, memory, skills, and Telegram bridge run on your device. Chelus sends context to DeepSeek for remote model inference. Local storage does not mean your prompts stay on the phone.</p>
<p>Additional command-line tools and services can extend the agent. They keep their own requirements: Chelus’s dependency-free Python core does not make an external integration dependency-free.</p>
<h2 id="terminal">A few terminal essentials</h2>
''' + code('chelus                    # resume the latest conversation\nchelus chat --new         # start fresh\nchelus sessions           # list conversations\nchelus show 3             # inspect a conversation\nchelus usage              # token and cache-hit totals') + r'''
<p>In chat, <code>/help</code> lists available commands. <code>/quit</code> ends the terminal chat, and Ctrl-C cancels a reply. A new conversation resets conversation-scoped ordinary tool approvals.</p>
<h2 id="source">For the curious</h2>
<p>Read the <a href="{{base}}reference/">reference</a> for commands, configuration defaults, file locations, and limits. The <a href="https://github.com/ClankClub/chelus/blob/main/docs/STATUS.md">project status</a> distinguishes automated coverage from live device verification.</p>
'''),
    'guide/telegram': page('Telegram', 'The phone stays home.\nThe conversation doesn’t.', 'A private chat with the agent running in Termux. The bridge polls Telegram over HTTPS; no public IP or open inbound ports required.', r'''
<h2 id="create-bot">Create your bot</h2>
<p>In Telegram, use <a href="https://t.me/BotFather">BotFather</a> to create a bot. Keep its token private. You’ll also need your numeric Telegram user ID, which Chelus uses to recognize the owner.</p>
<h2 id="configure">Save the credentials</h2>
<p>On the phone, create the configuration directory if needed. Paste the bot token at the hidden-input prompt, then press Enter. Replace <code>123456789</code> with your numeric user ID.</p>
''' + code('mkdir -p ~/.config/chelus && chmod 700 ~/.config/chelus\nread -rs TG\n(umask 077; printf \'%s\' "$TG" > ~/.config/chelus/telegram.token); unset TG\nprintf \'%s\' 123456789 > ~/.config/chelus/telegram_user_id') + r'''
<h2 id="start-bridge">Start the bridge</h2>
''' + code('chelus telegram') + r'''
<p>Open your bot’s private chat and send a message. The bridge must remain running on the device to answer messages and execute scheduled jobs.</p>
<div class="callout"><strong>Android can stop Termux.</strong><p>Background survival, Doze recovery, and automatic restart after reboot are not guaranteed. A wake lock and excluding Termux from battery optimization may help, but they are not a reliability guarantee.</p></div>
<h2 id="commands">Useful chat commands</h2>
<div class="table-scroll"><table><thead><tr><th>Command</th><th>Use it to</th></tr></thead><tbody><tr><td><code>/new</code></td><td>Start a fresh conversation.</td></tr><tr><td><code>/stop</code></td><td>Cancel the current turn or retry countdown.</td></tr><tr><td><code>/memory</code></td><td>Inspect persistent owner memory.</td></tr><tr><td><code>/learn [what]</code></td><td>Turn a procedure into a skill.</td></tr><tr><td><code>/skills</code>, <code>/pending</code></td><td>Inspect skills and proposed changes.</td></tr><tr><td><code>/approvals</code></td><td>Inspect or revoke ordinary tool grants.</td></tr><tr><td><code>/cron</code></td><td>Manage scheduled jobs without the model.</td></tr><tr><td><code>/stats</code></td><td>Toggle the runtime footer.</td></tr><tr><td><code>/help</code></td><td>See the available commands.</td></tr></tbody></table></div>
<h2 id="guests">Guests stay separate</h2>
<p>For guest access, put additional numeric user IDs in <code>~/.config/chelus/telegram_guests</code>, one per line, then restart the bridge. Guests have separate conversations, cannot access owner memory or skills, and cannot run risky tools. Guest requests still use your DeepSeek account.</p>
<p>File access for guests is further confined to the configured work folder. See <a href="{{base}}guide/tools-and-approvals/">tools and approvals</a> for the full boundary.</p>
'''),
    'guide/memory-and-skills': page('Memory & skills', 'Keep the useful parts.', 'Memory saves facts. Skills save procedures. Both live on your device, where you can inspect and manage them.', r'''
<h2 id="memory">Remember facts across conversations</h2>
<p>Tell Chelus something worth keeping: your preferred project folder, a working preference, or useful context. It can save owner-only facts directly and tells you what it saved. Proposed changes from background review wait for approval.</p>
''' + code('chelus memory\nchelus memory forget user 2') + r'''
<p>Use <code>/memory</code> in Telegram to inspect the same owner memory. The <code>forget</code> command above removes the specified entry; inspect the list first so you choose the right one.</p>
<p>Memory is deliberately small. It isn’t a limitless archive. Where SQLite FTS5 is available, Chelus can also search past conversations for relevant context.</p>
<h2 id="skills">Turn a workflow into a skill</h2>
<p>A skill is a readable <code>SKILL.md</code> procedure, optionally with reference files or scripts. Teach a repeatable workflow through instructions, files, or a recent conversation:</p>
''' + code('/learn how we prepare a release checklist', 'chat') + r'''
<p>Skill writes require approval by default. Background review may propose improvements after a conversation; they remain pending until you approve them.</p>
<h2 id="manage">Inspect, approve, and undo</h2>
''' + code('chelus skills\nchelus skills pending\nchelus skills view NAME\nchelus skills history NAME\nchelus skills undo NAME\nchelus skills pin NAME') + r'''
<p>Replace <code>NAME</code> with a listed skill. <code>undo</code> restores its previous revision. Pinning keeps a skill out of automatic curation. The weekly curator archives unused review-created skills, not skills you created or pinned.</p>
<h2 id="control">Keep control of what carries forward</h2>
<p>Owner and guest conversations don’t share history. Guests cannot reach owner memory or skills. Local memory and skills can become part of the context sent to DeepSeek, so avoid storing secrets in either.</p>
<p>For implementation details, read the source notes on <a href="https://github.com/ClankClub/chelus/blob/main/docs/hermes/memory.md">memory</a> and <a href="https://github.com/ClankClub/chelus/blob/main/docs/hermes/skills.md">skills</a>.</p>
'''),
    'guide/tools-and-approvals': page('Tools & approvals', 'Useful tools.\nAn explicit say-so.', 'Chelus can work with files, web pages, and shell commands. Risky actions ask for approval by default.', r'''
<h2 id="tools">What the agent can use</h2>
<div class="table-scroll"><table><thead><tr><th>Tool</th><th>What it does</th></tr></thead><tbody><tr><td><code>list_dir</code>, <code>read_file</code></td><td>Inspect permitted files; page through longer output.</td></tr><tr><td><code>search_files</code></td><td>Find file names or text by regex, with ripgrep when installed.</td></tr><tr><td><code>write_file</code>, <code>edit_file</code></td><td>Write a file or replace a specific piece of text, with approval.</td></tr><tr><td><code>run_shell</code></td><td>Run a shell command, with approval.</td></tr><tr><td><code>shell_jobs</code></td><td>List background commands, read logs, or stop a run.</td></tr><tr><td><code>web_fetch</code>, <code>web_search</code></td><td>Read public pages or search the web. Owner only.</td></tr><tr><td><code>battery_status</code></td><td>Read battery information when Termux:API is installed.</td></tr></tbody></table></div>
<p>You describe the task; the model selects tools. Availability still depends on your device, configuration, and optional packages.</p>
<h2 id="approvals">Choose the approval scope</h2>
<ul><li><strong>Allow once:</strong> approve that call only.</li><li><strong>Allow for this conversation:</strong> cover ordinary risky tools in this conversation, including later turns and after a restart. <code>/new</code> asks again.</li><li><strong>Always this action:</strong> remember the exact tool, normalized working directory, target, and effect arguments. A different command or content asks again.</li></ul>
<p>“Always” is not a command-prefix or execute-everything grant. Denied, unanswered, and allow-once requests save no grant. Skill approvals are separate. Telegram requests time out after three minutes.</p>
''' + code('chelus approvals\nchelus approvals revoke ID\nchelus approvals clear') + r'''
<p>Use <code>/approvals</code> in Telegram. Listings show grant IDs and scope without revealing stored commands or file contents.</p>
<h2 id="shell">Shell access is not a sandbox</h2>
<div class="callout"><strong>Approve commands with the device in mind.</strong><p>Approved shell commands run with Termux’s permissions. The file-tool protections are not a shell sandbox. The one-shot <code>chelus ask</code> command refuses risky tools unless you explicitly pass <code>--allow-risky</code>, and does not reuse saved ordinary grants.</p></div>
<p>File tools protect Chelus’s configuration and data directories and <code>~/.ssh</code>. Guests are additionally confined to <code>workdir</code> and cannot run risky tools.</p>
<h2 id="background">Let slower commands finish</h2>
<p>The foreground shell limit defaults to 120 seconds. Slower commands can run in the background for up to 30 minutes, with bounded logs and a finish notice from the Telegram bridge. At most three run at once. Jobs can survive a bridge restart, but not a reboot.</p>
<p>Long tool output is trimmed for context and saved temporarily for <code>read_file</code> to page through. Saved shell output masks credential-looking strings; do not rely on masking to make a secret safe to print.</p>
<h2 id="web">Web access</h2>
<p>Owner-only web tools need no approval. Fetching is restricted to public HTTP(S) text pages: no local networks, cloud metadata addresses, cookies, or credentials. Search uses DuckDuckGo’s HTML page, or Brave Search when a key is configured. Rate limits can prevent results.</p>
<p>The <a href="https://github.com/ClankClub/chelus/blob/main/docs/STATUS.md">verification notes</a> record which paths have been exercised live.</p>
'''),
    'guide/scheduling': page('Scheduled jobs', 'Give later a place.', 'Schedule a prompt or script, then receive the result in Telegram while the bridge is running.', r'''
<h2 id="create">Create a job</h2>
''' + code('chelus cron add --name tea "in 30m" Remind me the tea is ready\nchelus cron add "every day at 9am" Check the battery and tell me if it is under 30 percent') + r'''
<p>Options go before the schedule. Use <code>in 30m</code> for a one-shot job; <code>30m</code> or <code>every 2h</code> is recurring. Weekday schedules, five-field cron, and timestamps are also supported. Times use the phone’s local timezone.</p>
<p>You can also ask Chelus in chat to create a reminder. Its owner-only <code>cron</code> tool asks before creating, updating, resuming, running, or removing a job. Pausing needs no approval. The tool is unavailable inside scheduled runs.</p>
<h2 id="manage">Inspect, pause, or remove</h2>
''' + code('chelus cron\nchelus cron runs\nchelus cron pause JOB\nchelus cron resume JOB\nchelus cron run JOB\nchelus cron remove JOB') + r'''
<p>Replace <code>JOB</code> with a listed job ID. In Telegram, <code>/cron</code> and <code>/cron pause|resume|run|remove JOB</code> manage jobs without the model, so you can stop a noisy job during a provider outage. <code>run</code> queues it for the bridge’s next tick.</p>
<h2 id="permissions">Unattended means nobody can approve</h2>
<p>Each scheduled prompt starts with a fresh conversation. Risky tools can use only an existing “Always” grant for that exact action; denied tools are listed in the result. A reply of <code>[SILENT]</code> sends nothing.</p>
<p>Recurring messages include the next run and how to stop the job. Missed runs catch up once and are marked late. The bridge can carry delivered job context into your next conversation turn.</p>
<h2 id="scripts">Skip the model when a script will do</h2>
<p>Place a script under <code>~/.local/share/chelus/scripts/</code>, then create a script job. This example assumes you have written <code>battery.py</code>; it is not a bundled script.</p>
''' + code('chelus cron add --name battery --script battery.py --no-agent "every day at 9am"') + r'''
<p>With <code>--no-agent</code>, script output becomes the Telegram message and no model tokens are used. Without it, output becomes context for the prompt. Scripts run in their own process group from the work folder, with a five-minute limit and bounded output.</p>
<h2 id="reliability">Know the boundary</h2>
<div class="callout"><strong>The bridge is the clock.</strong><p>Jobs run only while it runs. Extended background operation, Doze recovery, and automatic restart after a reboot still need end-to-end verification. Don’t treat a Termux process as a guaranteed notification service.</p></div>
<p>Live device results and test-only paths are tracked in <a href="https://github.com/ClankClub/chelus/blob/main/docs/STATUS.md">project status</a>. The source <a href="https://github.com/ClankClub/chelus/blob/main/docs/hermes/cron.md">scheduler notes</a> cover the implementation.</p>
'''),
    'reference': page('Reference', 'The details, in reach.', 'Commands, configuration, local storage, and the boundaries worth knowing.', r'''
<h2 id="commands">Command reference</h2>
''' + code('chelus --help\nchelus chat --new\nchelus chat --session 3\nchelus ask "your question"\nchelus sessions\nchelus show 3\nchelus usage\nchelus memory\nchelus skills --help\nchelus approvals --help\nchelus cron --help\nchelus telegram') + r'''
<p>Exit codes: <code>0</code> success, <code>1</code> model/API error, <code>2</code> configuration error, <code>130</code> interrupted.</p>
<h2 id="configuration">Configuration</h2>
<p>Optional settings live in <code>~/.config/chelus/config.toml</code>. Unknown keys and incorrect types are errors. Start small:</p>
''' + code('model = "deepseek-flash"\nthinking = false\nreasoning_effort = "high"\nhistory_chars = 40000\nmax_iterations = 8\ncompaction = true\nskill_approval = "ask"', 'toml') + r'''
<p><code>reasoning_effort</code> accepts <code>low</code>, <code>high</code>, or <code>max</code> and is sent only when thinking is enabled. <code>skill_approval</code> defaults to <code>ask</code>; <code>off</code> applies skill writes directly.</p>
<p><code>workdir</code> is the starting folder for file tools and shell commands, and the guest file-access boundary. It defaults to your home directory; set an absolute path in <code>config.toml</code> to choose another folder.</p>
<h2 id="defaults">Defaults from the source</h2>
<p>This table is generated from the checked-in <a href="https://github.com/ClankClub/chelus/blob/main/chelus/config.py">Settings class</a> when the site builds.</p>
{{settings}}
<h2 id="files">Where things live</h2>
<div class="table-scroll"><table><thead><tr><th>Location</th><th>Contents</th></tr></thead><tbody><tr><td><code>~/.config/chelus/</code></td><td>Credentials, optional config.toml, and persona.md.</td></tr><tr><td><code>~/.local/share/chelus/</code></td><td>Conversation storage, memory, skills, and job state.</td></tr><tr><td><code>~/.cache/chelus/</code></td><td>Temporary cached output, including spill files.</td></tr></tbody></table></div>
<p><code>CHELUS_CONFIG_DIR</code>, <code>CHELUS_DATA_DIR</code>, and <code>CHELUS_CACHE_DIR</code> override those locations. <code>DEEPSEEK_API_KEY</code> overrides the key file. The old <code>CHELYS_CONFIG_DIR</code> and <code>CHELYS_DATA_DIR</code> variables remain supported during migration from the previous name.</p>
<p>A custom <code>persona.md</code> changes the agent’s personality. These are private runtime paths; they belong outside the Git checkout.</p>
<h2 id="platform">Platform &amp; inference</h2>
<p>Python 3.11 or newer is required. Chelus’s core has no pip dependencies or native Python extensions. The demonstrated environment is a Galaxy A10e with Android 11 and 32-bit Termux; other hardware and operating systems have not been established as supported.</p>
<p>Model inference is remote through DeepSeek. The local agent sends prompts, relevant conversation context, and tool results to the provider. Telegram is also an external service. This is not an offline or on-device-inference product.</p>
<h2 id="limits">Current limits &amp; release status</h2>
<p>The checked-in project is pre-1.0. Android can terminate Termux; unattended operation, missed-wake recovery, and reboot resilience are not guaranteed. Search availability depends on the upstream service.</p>
<p>The owner’s configured installation has demonstrated additional integrations, including email and deployment tools. Those are not all provided or documented by a fresh clone of the core.</p>
<p>Read <a href="https://github.com/ClankClub/chelus/blob/main/docs/STATUS.md">project status</a> for the current distinction between automated tests, tests executed on the phone, and live model/device use.</p>
<h2 id="attribution">License &amp; acknowledgments</h2>
<p>Chelus is MIT licensed and adapts ideas from <a href="https://github.com/NousResearch/hermes-agent">Hermes Agent</a>. It is independent of Nous Research. See the repository’s <a href="https://github.com/ClankClub/chelus/blob/main/NOTICE">NOTICE</a> for attribution.</p>
''', 'REFERENCE'),
}
