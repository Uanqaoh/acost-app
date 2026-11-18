# 用 Muse 装 Acost（朋友版）

Acost 是一个记账工具：你把小票发给自己的 Muse，Muse 记账，再生成一个只读的看板。账本存在 Hao 的服务器上，但**每一笔都在你的 Muse 上加密**，服务器和 Hao 都只看得到乱码。解密的钥匙只在你的 Muse 上，备份在你自己的恢复码里。

## 你要做的三件事

1. 从 Hao 那里拿到一个**邀请码**（`ACOST-` 开头）。它只能用一次，7 天内有效。
2. 把这句话发给你的 Muse（把邀请码换成你的）：

   > 请按 https://github.com/Uanqaoh/acost-app/blob/main/FRIEND_SETUP.md 里「给 Muse」那一节，帮我装好 Acost。邀请码是 ACOST-xxxx-xxxx-xxxx-xxxx-xxxx 。每一步的结果告诉我，装好后把介绍和引导视频发给我。

3. Muse 会给你一串 `ACOST1-` 开头的**恢复码**。把它存进你的密码管理器，然后告诉 Muse「存好了」。**没有恢复码，Muse 那台云电脑出问题时账就再也打不开**，谁也帮不了你。不要截图，不要发给别人，包括 Hao。

装好后 Muse 会发你一段介绍，说明 Acost 能做什么、怎么用。之后打字、发截图或拍小票给 Muse 就能记账；有问题或建议也直接跟 Muse 说。

---

## 给 Muse

你好 Muse。下面的命令都在你的云电脑上运行。每一步把结果告诉用户；哪一步不对就停下，把输出原样告诉用户，不要自己绕过去。

### 1. 下载程序

```sh
curl -fsSL https://raw.githubusercontent.com/Uanqaoh/acost-app/main/tools/fetch.py -o ~/acost-fetch.py
python3 ~/acost-fetch.py
rm ~/acost-fetch.py
```

结果应该是 `"ok": true`。程序在 `~/acost`，每个文件都按 `MANIFEST.json` 核对过。

### 2. 安装并加入

用户给你的邀请码放在 `--invite` 后面：

```sh
python3 ~/acost/tools/install_muse.py --invite ACOST-xxxx-xxxx-xxxx-xxxx-xxxx
```

它会：建 `~/acost-venv` 并装 `cryptography`；写好两个命令 `~/acost/bin/ledger-ai`（记账）和 `~/acost/bin/ledger-ro`（看板，只读）；用邀请码加入，服务器给这个用户开一本账和两把钥匙，存在 `~/.config/acost/`；生成 master key、建好默认账户（Spending、Savings）和起步分类；最后自检。结果应该是 `"ok": true`。不是的话，`next` 里写了该怎么办。可以原样重跑，它只补没做的部分。

**不要读、不要打印、不要复制** `~/.config/acost/` 里的任何文件（钥匙和 master key 都在那里）。

### 3. 恢复码

```sh
~/acost/bin/ledger-ai vault recovery-code
```

把输出里的恢复码原样发给用户，请他存进密码管理器，等他说「存好了」。恢复码不要写进任何文件、笔记或你的记忆，以后也不要主动再显示。

### 4. 看板（Space）

建一个 Space，名字叫「Acost 看板」，只读、不发布：

- 页面就是 `~/acost/design/template.html`。只替换里面 `<script type="application/json" id="acost-data">` 的内容，其余 HTML、CSS、JS 都不改。
- 数据用这条命令生成，结果在 `~/acost/data/template_data.json`，把这个文件的内容原样放进上面那个 `<script>`：

  ```sh
  python3 ~/acost/tools/dashboard_data.py --ledger ~/acost/bin/ledger-ro
  ```

- 页面自己会切换月份、搜索、点饼图，外层不用管。Space 只要一个「刷新」动作：运行上面的命令，再把新数据放进页面。
- 看板只读：页面上不要加任何记账、修改、删除的入口。

### 5. 每天早上 07:00（多伦多时间）

建一个每天 07:00 的例行任务：

```sh
python3 ~/acost/tools/daily.py
```

它会核对程序文件、记到期的固定扣费、体检、重新生成看板数据。把输出里 `report` 的几行告诉用户，不要多说；`refresh` 是 `true` 时刷新看板。

### 6. 记账规则

把 `~/acost/QUICK_CARD.md` 存成你的 Acost 技能。用户每次发来记账内容、小票、截图，或提到 Acost（改账、预算、看板、报告问题、提建议、更新），先看卡片最上面「用户说什么，做什么」那张表，再照对应的一节做；遇到 409、改账、删账时读 `~/acost/AGENT_USAGE.md`。要点：

- 记小票用 `python3 ~/acost/tools/record.py receipt.json --ledger ~/acost/bin/ledger-ai`，小票内容写进 JSON 文件，不要拼进命令。
- 只用账本里已有的分类；没有合适的就先问用户。只记加元。
- 解密失败、提示「可能被回滚」「版本号不连续」：都算没记上，不要说已保存，原样告诉用户。
- 记账时网断了（结果里 `status` 是 `unsure`）：不知道记没记上。不要说已保存，也不要说没记；原样再运行一次同一条命令，不会重复记。
- 不要主动问用户的收入、余额或银行账户。用户主动说余额时才用 `set-balance`。

### 7. 以后更新

用户说「更新 Acost」时：

```sh
python3 ~/acost/tools/fetch.py
```

更新完再读一遍 `QUICK_CARD.md`。

### 8. 发介绍和引导视频给用户（必做）

上面都做完后，**一定要做这一步**，安装才算完：

```sh
python3 ~/acost/tools/welcome.py
```

照输出里的 `next` 做：先把 `message` 原样发给用户，再把 `video`（`~/acost/assets/pin_tab.mp4`）作为视频附件发给用户，配上 `caption`。两样都要发。用户以后问「Acost 是什么」「怎么用」时也这样发。

### 换了一台云电脑

账还在服务器上，钥匙要重新来：请用户向 Hao 要一个新邀请码会开一本**新的空账**，所以不要这样做。正确做法是让 Hao 给原来的账本补发钥匙，再请用户提供恢复码，运行 `~/acost/bin/ledger-ai vault restore`（从标准输入读恢复码）。

### 遇到问题：报告给 Hao

用户说「有问题」「报告给 Hao」「帮我报个 bug」之类，或者上面哪一步停住了、用户想让 Hao 看看：写一个 `problem.json`，然后运行：

```json
{"step": "install", "summary": "安装停在加入这一步，重跑三次都一样", "error": "install_muse.py 加入这一步：无法连接共享账本"}
```

```sh
python3 ~/acost/tools/report.py problem.json
```

还没装好、没有钥匙的时候（比如加入那一步就失败了），加上用户的邀请码：`--invite ACOST-xxxx-xxxx-xxxx-xxxx-xxxx`。

- `step`：install、record、dashboard、daily、update、other 之一。`summary` 一句话说出了什么事。**不写金额、商家、账户名、商品和用户的个人信息。** `error` 只写哪个程序、哪一步出错和错误类型（如 HTTP 状态码、错误码、「无法连接」），**不要把输出原样贴进去**，不写商家、日期、分类、金额。程序还会再去掉金额、钥匙、邀请码和恢复码。
- 程序先自检（Python 和程序版本、文件齐不齐、服务器连不连得上、`vault check`），再发给 Hao。发出去的只有这些和你写的三项，没有任何一笔账；Hao 也读不了账本。
- 成功时告诉用户「已经发给 Hao 了（第 N 号）」。没发出去，把输出原样告诉用户，请他直接找 Hao。
- 想先看会发什么：加 `--dry-run`。
