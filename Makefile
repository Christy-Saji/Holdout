.PHONY: eval report sweep test repro

eval:
	python -m recovery.cli eval --seed 42 --n 500

report:
	python -m recovery.cli report

sweep:
	python -m recovery.cli sweep --param p_self_heal --range 0.5,1.5

test:
	pytest

repro:
	python -m recovery.cli repro
