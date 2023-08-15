#curl -sSL https://install.python-poetry.org | python3
export PATH="/home/fedow/.local/bin:$PATH"
poetry install
echo "Poetry install ok"
#echo "sleep infinity"
#sleep infinity
#poetry shell
poetry run python3 manage.py migrate
poetry run python3 manage.py collectstatic --noinput
echo "Run GUNICORN"
poetry run python3 manage.py 0.0.0.0:8000
#poetry run gunicorn fedowallet_django.wsgi --log-level=debug --log-file /fedow/www/gunicorn.logs -w 3 -b 0.0.0.0:8000