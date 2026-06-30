up:
	docker compose up -d redis api worker flower

down:
	docker compose down

build:
	docker compose build api worker

rebuild:
	docker compose build api worker && docker compose up -d api worker

logs:
	docker compose logs -f api worker

shell:
	docker compose exec api bash
