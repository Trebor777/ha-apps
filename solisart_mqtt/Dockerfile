ARG BUILD_FROM
FROM $BUILD_FROM

ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir --break-system-packages "paho-mqtt>=1.6"

COPY solisart.py solisart_mqtt.py run.sh /app/
RUN chmod a+x /app/run.sh

WORKDIR /app
CMD ["/app/run.sh"]
