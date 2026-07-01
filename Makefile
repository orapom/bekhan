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

backup:
	docker compose exec api python backup.py

backup-full:
	docker compose exec api python backup.py --media
