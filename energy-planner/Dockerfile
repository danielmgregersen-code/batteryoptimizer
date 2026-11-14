ARG BUILD_FROM
FROM $BUILD_FROM

# tzdata: hour-of-day profiles and the UI must use the local time Supervisor passes in TZ.
# libstdc++/libgcc: the musllinux numpy/scipy wheels link them dynamically (HiGHS is C++).
RUN apk add --no-cache tzdata libstdc++ libgcc

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY app/ .

# Launch via with-contenv (run.sh) so SUPERVISOR_TOKEN is available at runtime
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8010

CMD ["/run.sh"]
