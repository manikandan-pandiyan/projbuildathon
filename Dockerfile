# Mech Royale bot. Standard library only, so nothing to install.
FROM python:3.13-slim
WORKDIR /bot
COPY mechfight.py worldmap.py bot.py ./
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
