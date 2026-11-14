# deploy

systemd units on billmill.org that start the GitHub workflows through the
`workflow_dispatch` API; GitHub's own cron dropped and delayed too many runs
(see docs/v2.md). `nba-dispatch@.service` dispatches the workflow file it's
given (`nba-dispatch@update.yml.service`), and each workflow has a timer:

- `nba-update.timer`: `update.yml`, every 30 minutes from 19:05 to 11:35 UTC, while games end; most runs stop at the gate (`pipeline.gate`) because no game is due
- `nba-update-full.timer`: `update.yml` with `full=true` (through `nba-dispatch-full@.service`), 12:43 and 16:43 UTC: runs regardless, refetches recent play-by-play and the rosters
- `nba-sources.timer`: `sources.yml`, 12:03 UTC
- `nba-injuries.timer`: `injuries.yml`, shortly before each cluster of tip-offs (11:40 am to 10:15 pm Eastern; the workflow skips a run with no game in the next 75 minutes)

To install them on a new server, as root:

```sh
install -m 600 /dev/null /etc/nba-dispatch-token   # then paste a fine-grained token: Actions read/write on llimllib/nba_data
cp nba-dispatch@.service nba-dispatch-full@.service nba-update.timer nba-update-full.timer nba-sources.timer nba-injuries.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nba-update.timer nba-update-full.timer nba-sources.timer nba-injuries.timer
systemctl start nba-dispatch@update.yml.service   # test; journalctl -u 'nba-dispatch@*' shows errors
```

After changing a file here, copy it to the server and run
`systemctl daemon-reload`.
