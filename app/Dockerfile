FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# engine package lives one level up from app/
COPY sdoc /srv/sdoc
COPY app /srv/app
ENV SDOC_DATA_ROOT=/srv/data
EXPOSE 8000
WORKDIR /srv/app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
