FROM public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app
COPY main.py planner.py ./
EXPOSE 8080
CMD ["python", "-u", "main.py"]
