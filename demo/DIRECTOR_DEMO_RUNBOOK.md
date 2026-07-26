# Director Demo Runbook

## Before the call (15 minutes)

1. Open <https://tracker.queuecraft-dev.com> in a fresh browser tab.
2. Set **Group** to `Demo` and select **Director Demo Roadmap**.
3. Keep Discord open to the bot's test/demo channel.
4. In a non-demo channel, run these visual checks as a server manager:
   - `/test super visual:true`
   - `/test config visual:true`
   - `/test plan visual:true`
   - `/test panel visual:true`
5. If those messages make the test channel noisy, run `/clear-bot-messages` there afterward.

## The prepared storyline

The `Director Demo Roadmap` is intentionally in a useful mid-project state:

- **Define the outcome** is complete.
- **Run the interactive walkthrough** is available and has one task in progress.
- **Prepare the pilot** and **Start the team pilot** are visibly locked behind that work.

## Suggested 8-minute walkthrough

1. **Start on the dashboard.** Point out the six-hole animated Bev, the `Demo` filter, difficulty bars, completed work, the available next step, and locked future work.
2. **Open the interactive project in Discord.** Use `/task list project:Interactive Walkthrough` to show the three tasks and their IDs.
3. **Show work moving.** Complete the in-progress task with `/task done task_id:<id>`. Then complete the remaining two tasks. Each change updates the project progress.
4. **Show the unlock.** Run `/tree show tree:director-demo` or refresh the dashboard. Completing the walkthrough opens **Prepare the pilot**.
5. **Show planning outside Discord.** Use the dashboard header to open **Tree planner**. Explain: build a larger plan offline, download it, then use `/tree import` to preview and apply it.
6. **Show governance and reporting.** Open **Config panel** from the header, then download the project CSV from the dashboard's Reports section.

## If something goes wrong

- The live data backup is on the VPS at `/opt/discord-project-tracker/backups/tracker-pre-director-demo-20260726T204307Z.db`.
- The bot and dashboard are configured to restart automatically after a failure or reboot.
- If the site does not load, first check `https://tracker.queuecraft-dev.com/healthz`. It should say `ok`.
- Keep the dashboard tab open as the visual fallback; it contains the prepared roadmap and downloadable reports.

## One sentence to anchor the demo

“This turns project tracking into a shared roadmap: people see what is active now, what unlocks next, and where work is actually blocked.”
