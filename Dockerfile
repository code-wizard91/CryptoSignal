

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
