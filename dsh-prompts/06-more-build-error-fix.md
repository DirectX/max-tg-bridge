Ошибки при сборке на orangepi:

```
docker compose up -d --build
[+] Building 18.1s (7/17)                                                                                                                                                    
 => [internal] load local bake definitions                                                                                                                              0.0s
 => => reading from stdin 567B                                                                                                                                          0.0s
 => [internal] load build definition from Dockerfile                                                                                                                    0.0s
 => => transferring dockerfile: 6.07kB                                                                                                                                  0.0s
 => [internal] load metadata for mcr.microsoft.com/playwright/python:v1.49.0-jammy                                                                                      1.8s
 => [internal] load .dockerignore                                                                                                                                       0.0s
 => => transferring context: 182B                                                                                                                                       0.0s
 => [ 1/12] FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy@sha256:70c06141ac9041eedef4a68387ea978ceb090c37bd5b66583353b7fe17b9f3d7                             15.5s
 => => resolve mcr.microsoft.com/playwright/python:v1.49.0-jammy@sha256:70c06141ac9041eedef4a68387ea978ceb090c37bd5b66583353b7fe17b9f3d7                                0.0s
 => => sha256:baebe3306686caa5ff5e4e43672eaef8adaedfee4f4ab626b9a77f89c78cfadc 49.28MB / 720.88MB                                                                      15.9s
 => => sha256:5af8f21004f317a063f2f154eb7da66e16d13468bf4eabaf1f30a56c8262434c 34.60MB / 87.25MB                                                                       15.9s
 => => sha256:e2a2044e36ee5b79eb1dca9656e04f662b1b08154d08c6d7cdcb8be95dc2e212 43.32MB / 43.32MB                                                                       12.1s
 => => sha256:4be1db8bbbebdd00e047c599d9aa2ee2ac533600bee2ac25a86573e42598d326 28.40MB / 28.40MB                                                                       13.5s
 => => extracting sha256:4be1db8bbbebdd00e047c599d9aa2ee2ac533600bee2ac25a86573e42598d326                                                                               1.9s
 => => extracting sha256:e2a2044e36ee5b79eb1dca9656e04f662b1b08154d08c6d7cdcb8be95dc2e212                                                                               0.0s
 => [internal] load build context                                                                                                                                       0.1s
 => => transferring context: 373.37kB                                                                                                                                   0.0s
 => ERROR [ 2/12] WORKDIR /app                                                                                                                                          0.0s
------
 > [ 2/12] WORKDIR /app:
------
Dockerfile:39

--------------------

  37 |         PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

  38 |     

  39 | >>> WORKDIR /app

  40 |     

  41 |     #--- Системные дополнения ----------------------------------------------------

--------------------

failed to solve: failed to compute cache key: failed to mount /home/orangepi/.local/share/docker/containerd/daemon/tmpmounts/containerd-mount2502283490: mount source: "overlay", target: "/home/orangepi/.local/share/docker/containerd/daemon/tmpmounts/containerd-mount2502283490", fstype: overlay, flags: 0, data: "workdir=/home/orangepi/.local/share/docker/containerd/daemon/io.containerd.snapshotter.v1.overlayfs/snapshots/50/work,upperdir=/home/orangepi/.local/share/docker/containerd/daemon/io.containerd.snapshotter.v1.overlayfs/snapshots/50/fs,lowerdir=/home/orangepi/.local/share/docker/containerd/daemon/io.containerd.snapshotter.v1.overlayfs/snapshots/49/fs,index=off", err: operation not permitted
```
При сборке на хосте x86_64:

```
docker compose up -d --build
[+] Building 0.6s (8/17)                                                        
 => [internal] load local bake definitions                                 0.0s
 => => reading from stdin 575B                                             0.0s
 => [internal] load build definition from Dockerfile                       0.0s
 => => transferring dockerfile: 6.07kB                                     0.0s
 => [internal] load metadata for mcr.microsoft.com/playwright/python:v1.4  0.2s
 => [internal] load .dockerignore                                          0.0s
 => => transferring context: 182B                                          0.0s
 => [ 1/12] FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy@sha256  0.0s
 => => resolve mcr.microsoft.com/playwright/python:v1.49.0-jammy@sha256:7  0.0s
 => [internal] load build context                                          0.0s
 => => transferring context: 3.40kB                                        0.0s
 => CACHED [ 2/12] WORKDIR /app                                            0.0s
 => ERROR [ 3/12] RUN apt-get update  && apt-get install -y --no-install-  0.2s
------                                                                          
 > [ 3/12] RUN apt-get update  && apt-get install -y --no-install-recommends openssl x11vnc  && rm -rf /var/lib/apt/lists/*:
0.185 exec /bin/sh: exec format error
------
[+] up 0/1
 ⠙ Image max-tg-bridge:2 Building                                           0.7s
Dockerfile:45

--------------------

  44 |     # x11vnc   — «глазки» для ручного прохождения капчи/QR (опция).

  45 | >>> RUN apt-get update \

  46 | >>>  && apt-get install -y --no-install-recommends openssl x11vnc \

  47 | >>>  && rm -rf /var/lib/apt/lists/*

  48 |     

--------------------

failed to solve: process "/bin/sh -c apt-get update  && apt-get install -y --no-install-recommends openssl x11vnc  && rm -rf /var/lib/apt/lists/*" did not complete successfully: exit code: 255
```