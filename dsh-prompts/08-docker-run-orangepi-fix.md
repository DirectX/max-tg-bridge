Два вопроса:

1. На рабочей станции теперь запускается и бот отвечает, но несмотря на правильно прописанный id администратора

.env
...
# Telegram user id СУПЕР-АДМИНИСТРАТОРА: единственный, кто видит /admin,
# ведёт авторизацию и раздаёт права. Узнать свой: /start боту после запуска.
ADMIN_TG_ID=123456789
...

После запуска /start выдаётся сообщение:

```
🤖 Мост MAX ⇄ Telegram v2.
Ваш user id: `123456789`
Аккаунтов за вами не закреплено — вас должен добавить администратор.
```

На orangepi докер всё также падает на этапе сборки:

docker compose up -d --build 
[+] Building 1.1s (7/17)                                                                                                                                                     
 => [internal] load local bake definitions                                                                                                                              0.0s
 => => reading from stdin 515B                                                                                                                                          0.0s
 => [internal] load build definition from Dockerfile                                                                                                                    0.0s
 => => transferring dockerfile: 6.74kB                                                                                                                                  0.0s
 => [internal] load metadata for mcr.microsoft.com/playwright/python:v1.49.0-noble                                                                                      0.7s
 => [internal] load .dockerignore                                                                                                                                       0.0s
 => => transferring context: 182B                                                                                                                                       0.0s
 => [ 1/12] FROM mcr.microsoft.com/playwright/python:v1.49.0-noble@sha256:35a390e70ab8ba7fbc909dc01af2329e287e89ccc8fb2f04a1a7ae94cacc5c30                              0.1s
 => => resolve mcr.microsoft.com/playwright/python:v1.49.0-noble@sha256:35a390e70ab8ba7fbc909dc01af2329e287e89ccc8fb2f04a1a7ae94cacc5c30                                0.0s
 => => extracting sha256:add8217a8dd66f014d5fa2b351e1cd48a27adb23e1cf15ea7c619509dcb6dc65                                                                               0.0s
 => => sha256:33e7d00b251205ddb2560339d46b96636c39c164d5fd60a6e16047192f3306f9 0B / 739.18MB                                                                            0.1s
 => [internal] load build context                                                                                                                                       0.0s
 => => transferring context: 1.38kB                                                                                                                                     0.0s
 => ERROR [ 2/12] WORKDIR /app                                                                                                                                          0.0s
------
 > [ 2/12] WORKDIR /app:
------
Dockerfile:40

--------------------

  38 |         PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

  39 |     

  40 | >>> WORKDIR /app

  41 |     

  42 |     #--- Системные дополнения (опционально) --------------------------------------

--------------------

failed to solve: failed to compute cache key: failed to mount /home/orangepi/.local/share/docker/containerd/daemon/tmpmounts/containerd-mount3241509558: mount source: "overlay", target: "/home/orangepi/.local/share/docker/containerd/daemon/tmpmounts/containerd-mount3241509558", fstype: overlay, flags: 0, data: "workdir=/home/orangepi/.local/share/docker/containerd/daemon/io.containerd.snapshotter.v1.overlayfs/snapshots/55/work,upperdir=/home/orangepi/.local/share/docker/containerd/daemon/io.containerd.snapshotter.v1.overlayfs/snapshots/55/fs,lowerdir=/home/orangepi/.local/share/docker/containerd/daemon/io.containerd.snapshotter.v1.overlayfs/snapshots/52/fs,index=off", err: operation not permitted
