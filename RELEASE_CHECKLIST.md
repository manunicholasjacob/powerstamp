# Release checklist

Everything below is a manual step for Manu. None of it has been done. No
external action was taken while this repository was built: nothing was pushed,
no repository was created, nothing was published, no issue or pull request was
opened, no message was sent, and nothing was spent. The repository exists only
on this machine, with local commits.

## 0. Before anything is public

- [ ] **Confirm the name.** Checked on 1 November 2026:
      `https://pypi.org/pypi/powerstamp/json` returned 404, so the PyPI name is
      free, and `https://github.com/manunicholasjacob/powerstamp` returned 404,
      so the repository name is free. A GitHub code search for `powerstamp`
      returned two unrelated projects, `Mankua/powerstamper` (a 3ds Max plug-in)
      and `vivainsider/powerstamp-map`. PyPI normalises `power-stamp` and
      `power_stamp` to the same name, so the 404 covers those spellings.
      Re-check both immediately before publishing, since availability changes.
- [ ] **Re-read the honesty claims.** The README states that none of the nine
      related tools does all four of achieved rate, rail coverage, uncertainty
      and refusal. That survey was done on 1 November 2026 against each tool's
      own source or documentation. If any of them has since added one of the
      four, the README's comparison table and the sentence about what
      `powerstamp` adds both need updating before the claim goes public.
- [ ] **Decide on the 30 sample threshold.** It is a defended judgement, not a
      derivation. If you want a different number, change `MIN_SAMPLES` in
      `src/powerstamp/uncertainty.py`, update the docstring's rationale, and
      update the refusal rule section of the README. The tests assert the value
      is 30 and will fail until they are updated too, which is intentional.

## 1. Validate on real hardware

The test suite runs against synthetic fixtures, so the logic is tested and
agreement with real hardware is not. The README says so. Do at least the first
two of these before telling anyone the tool works.

- [ ] **x86 Linux with RAPL.** As root, `powerstamp detect` and
      `powerstamp run --sample-hz 20 -- <workload>`. Check that the domains
      listed in `measurement.domains_read` match `ls /sys/class/powercap/`, that
      `unreadable` is empty, and that the joule figure is within a plausible
      range of `perf stat -e power/energy-pkg/ <workload>` over the same work.
      Record the comparison; it is the single most useful validation number this
      tool can have.
- [ ] **The same machine as a non-root user.** Confirm the detection message
      names the root-only restriction and cites the commit, and that the verdict
      is `UNSUPPORTED` rather than a zero.
- [ ] **Raspberry Pi 5.** Confirm the live `vcgencmd pmic_read_adc` output has
      the same shape as `tests/fixtures/pmic_read_adc_pi5.txt`, that
      `rail_count` is 12 and `channel_reads_per_sample` is 26 on your unit, and
      that the achieved sweep rate lands near the 21.2 Hz figure from the
      earlier work. If your unit differs, update the fixture and say in the
      README that the structure varies by device.
- [ ] **NVIDIA GPU, Volta or newer.** Confirm `energy_source` is
      `nvmlDeviceGetTotalEnergyConsumption` and compare against
      `nvidia-smi --query-gpu=power.draw` integrated over the same window. The
      two should be close and will not be identical, which is the point of the
      `ENERGY_FROM_POWER_INTEGRAL` versus `ENERGY_FROM_HARDWARE_COUNTER`
      distinction.
- [ ] **NVIDIA GPU older than Volta**, if you still have access to one, to
      exercise the fallback for real.
- [ ] **Android.** Remember the standing rule from the Galaxy M21 work: check
      the process table before measuring. Confirm the `BATTERY_SIDE_READING`
      caveat appears and that the gauge produces enough distinct counter values
      over a run of a realistic length.

## 2. Local checks

- [ ] `python -m pytest -v` passes. It was 135 passed on Windows with Python
      3.14.6 when this was written.
- [ ] `LC_ALL=C grep -rn '[^ -~]' --exclude-dir=.git .` prints nothing.
- [ ] No em dashes anywhere: `grep -rn -- '--' .` for a visual check of the
      double-hyphen cases, and the ASCII check above already rules out the real
      character.
- [ ] `python -m build` then `python -m twine check dist/*` both clean.
- [ ] `pip install dist/powerstamp-0.1.0-py3-none-any.whl` into a fresh virtual
      environment, then `powerstamp --version` and `powerstamp detect` work from
      the console script rather than `python -m`.
- [ ] Check the outgoing hygiene of the files that will be public: invisible
      Unicode and metadata.
- [ ] Confirm no commit contains a `Co-Authored-By: Claude` trailer and no file
      contains "Generated with Claude Code". `git log --format='%B' | grep -i
      claude` should print nothing.

## 3. Create the repository

- [ ] `gh repo create manunicholasjacob/powerstamp --public --source=. --remote=origin`
      or create it in the web UI and add the remote.
- [ ] Set the description to the one-liner from `pyproject.toml` and add the
      topics: `energy`, `power-measurement`, `uncertainty`, `rapl`, `nvml`,
      `raspberry-pi`, `benchmarking`, `reproducibility`.
- [ ] `git push -u origin main`.
- [ ] Watch the CI run. The `test` matrix should be green everywhere. The
      `rapl-linux` job reports and does not assert, so read its output rather
      than trusting the tick: a GitHub hosted runner is a VM without a readable
      `energy_uj`, so the expected result there is an honest unavailable with a
      reason. The `action` job runs the Action on itself with `fail-on: none`
      for the same reason.

## 4. Tag and publish

The tag is what makes `manunicholasjacob/powerstamp@v0.1.0` usable as an Action,
so it matters more than the PyPI upload for the dependents goal.

- [ ] `git tag -a v0.1.0 -m "powerstamp 0.1.0"` then `git push origin v0.1.0`.
- [ ] Create the GitHub release from the tag, with the 0.1.0 section of
      `CHANGELOG.md` as the body and the built wheel and sdist attached.
- [ ] Also move a `v0.1` or `v1` floating tag if you want the usual Action
      convention. Decide once and document it in the README, because moving a
      tag after people depend on it is worse than not having one.
- [ ] Upload to PyPI. Use TestPyPI first:
      `python -m twine upload --repository testpypi dist/*`, install from there
      into a clean environment, then `python -m twine upload dist/*`.
      Use an API token, not a password.
- [ ] After the upload, confirm the PyPI page renders the README correctly and
      that the license and author fields are right.

## 5. Make the dependents appear

GitHub publishes a "Used by" list only for a package another repository names in
`pyproject.toml` or `requirements.txt`, and for an Action another repository
names in a workflow. Both shapes ship here, so the work left is getting real
repositories to name them.

- [ ] Add `powerstamp` to the dependency list of your own repositories where it
      genuinely belongs, and add the Action to their workflows where an energy
      number is actually wanted. Do not add it where it does nothing; a
      dependent that does not use the tool is noise, and it will read as noise.
- [ ] If and when the sibling tools exist, have them name each other where that
      is honest.
- [ ] Do not claim adoption that has not happened. There is no adoption yet, and
      the README says nothing that implies otherwise.

## 6. Do not do these

- [ ] Do not propose contributing this to Zeus. A prior pull request to that
      project was closed. Zeus is cited as related work and nothing more.
- [ ] Do not accept any bounty, cash prize, honorarium or payout connected to
      this work until an attorney has cleared it.
- [ ] Do not pay an article processing charge if this ends up attached to a
      paper.

## 7. If a paper cites this

- [ ] Deposit a Zenodo DOI from the GitHub release and add the DOI to
      `CITATION.cff` and the README.
- [ ] If the tool is used to produce a number in a submission, state the
      achieved sampling rate, the rails covered and the expanded uncertainty in
      the paper, since that is the entire argument for the tool existing.
