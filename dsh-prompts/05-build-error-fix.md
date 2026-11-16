docker compose up -d --build
[+] Building 1.3s (8/17)                                                                                                                             
 => [internal] load local bake definitions                                                                                                      0.0s
 => => reading from stdin 575B                                                                                                                  0.0s
 => [internal] load build definition from Dockerfile                                                                                            0.0s
 => => transferring dockerfile: 5.56kB                                                                                                          0.0s
 => [internal] load metadata for mcr.microsoft.com/playwright/python:v1.49.0-jammy                                                              0.8s
 => [internal] load .dockerignore                                                                                                               0.0s
 => => transferring context: 182B                                                                                                               0.0s
 => [ 1/12] FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy@sha256:70c06141ac9041eedef4a68387ea978ceb090c37bd5b66583353b7fe17b9f3d7      0.0s
 => => resolve mcr.microsoft.com/playwright/python:v1.49.0-jammy@sha256:70c06141ac9041eedef4a68387ea978ceb090c37bd5b66583353b7fe17b9f3d7        0.0s
 => [internal] load build context                                                                                                               0.0s
 => => transferring context: 3.40kB                                                                                                             0.0s
 => CACHED [ 2/12] WORKDIR /app                                                                                                                 0.0s
 => ERROR [ 3/12] RUN apt-get update  && apt-get install -y --no-install-recommends openssl x11vnc  && rm -rf /var/lib/apt/lists/*              0.2s
------                                                                                                                                               
 > [ 3/12] RUN apt-get update  && apt-get install -y --no-install-recommends openssl x11vnc  && rm -rf /var/lib/apt/lists/*:
0.189 exec /bin/sh: exec format error
------
[+] up 0/1
 ⠙ Image max-tg-bridge:2 Building                                                                                                                1.3s
Dockerfile:39

--------------------

  38 |     # x11vnc   — «глазки» для ручного прохождения капчи/QR (опция).

  39 | >>> RUN apt-get update \

  40 | >>>  && apt-get install -y --no-install-recommends openssl x11vnc \

  41 | >>>  && rm -rf /var/lib/apt/lists/*

  42 |     

--------------------

failed to solve: process "/bin/sh -c apt-get update  && apt-get install -y --no-install-recommends openssl x11vnc  && rm -rf /var/lib/apt/lists/*" did not complete successfully: exit code: 255