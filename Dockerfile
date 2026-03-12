FROM python:3.11

WORKDIR /app

COPY . .

RUN pip install flask python-pam

EXPOSE 8080

CMD ["python","app.py"]