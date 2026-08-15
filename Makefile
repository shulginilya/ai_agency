# Thin shim over tasks.py so `make <target>` and `python tasks.py <target>` behave
# identically. All logic lives in tasks.py -- keep it that way.
PY ?= python3

.PHONY: install run repl team serve doctor lint fmt typecheck test eval check docker-build docker-run clean

install:
	$(PY) tasks.py install

run:
	$(PY) tasks.py run "$(P)"

repl:
	$(PY) tasks.py repl

team:
	$(PY) tasks.py team "$(P)"

serve:
	$(PY) tasks.py serve $(ARGS)

doctor:
	$(PY) tasks.py doctor

lint:
	$(PY) tasks.py lint

fmt:
	$(PY) tasks.py fmt

typecheck:
	$(PY) tasks.py typecheck

test:
	$(PY) tasks.py test

eval:
	$(PY) tasks.py eval

check:
	$(PY) tasks.py check

docker-build:
	$(PY) tasks.py docker-build

docker-run:
	$(PY) tasks.py docker-run "$(P)"

clean:
	$(PY) tasks.py clean
