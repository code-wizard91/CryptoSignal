FROM python:2.7

RUN mkdir -p /cryptosignal
ADD ./requirements.txt /cryptosignal/requirements.txt
WORKDIR /cryptosignal
RUN pip install -r requirements.txt

RUN echo America/New_York | tee /etc/timezone && dpkg-reconfigure --frontend noninteractive tzdata

RUN apt-get update && apt-get upgrade -y
RUN apt-get install -y supervisor openssh-server
RUN apt-get install -y screen

RUN mkdir /root/.ssh
ADD authorized_keys /root/.ssh/authorized_keys

RUN /bin/echo -e "#!/bin/bash\n\
sed -ri 's/UsePAM yes/#UsePAM yes/g' /etc/ssh/sshd_config && sed -ri 's/#UsePAM no/UsePAM no/g' /etc/ssh/sshd_config\n\
service ssh start\n\
exec >/dev/tty 2>/dev/tty </dev/tty\n\
cd /cryptosignalr && screen -s /bin/bash -dmS cryptosignal ./cryptosignal.py --strategy=balancer\n\
" > /cryptosignal/launch-pytrader.sh
RUN chmod +x /cryptosignal/launch-cryptosignal.sh

# Setup supervisord
RUN /bin/echo -e "[supervisord]\n\
nodaemon=true\n\
\n\
[program:cryptosignal]\n\
directory=/cryptosignalr\n\
user=root\n\
command=/cryptosignal/launch-cryptosignal.sh\n\
startsecs=0" > /etc/supervisor/conf.d/cryptosignal.conf

# Add "screen -r" to .profile
RUN /bin/echo -e "\n\
cd /cryptosignal\n\
screen -r\n\
" >> /root/.profile

ADD . /pytrader
EXPOSE 22
CMD ["-n", "-c", "/etc/supervisor/conf.d/cryptosignal.conf"]
ENTRYPOINT ["/usr/bin/supervisord"]
