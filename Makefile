.PHONY: test bench serve docker-up

test:
	pytest -v

bench:
	python3 -m eval.run_benchmark --split held_out --repeats 10

serve:
	uvicorn tripgraph.api.main:app --host 0.0.0.0 --port 8000

docker-up:
	docker compose up --build
