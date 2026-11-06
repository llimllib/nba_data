# deploy

systemd units on billmill.org that start the GitHub workflows through the
`workflow_dispatch` API; GitHub's own cron dropped and delayed too many runs
(see docs/v2.md). `nba-dispatch@.service` dispatches the workflow file it's
given (`nba-dispatch@update.yml.service`), and each workflow has a timer:

- `nba-update.timer`: `update.yml`, every 4 hours at :43 plus 06:13 and 10:13 UTC
- `nba-sources.timer`: `sources.yml`, 11:17 UTC

To install them on a new server, as root:

```sh
install -m 600 /dev/null /etc/nba-dispatch-token   # then paste a fine-grained token: Actions read/write on llimllib/nba_data
cp nba-dispatch@.service nba-update.timer nba-sources.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nba-update.timer nba-sources.timer
systemctl start nba-dispatch@update.yml.service   # test; journalctl -u 'nba-dispatch@*' shows errors
```

After changing a file here, copy it to the server and run
`systemctl daemon-reload`.
