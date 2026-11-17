// ==UserScript==
// @name         OCS 题库助手 · 抓答案 + 路线切换
// @namespace    ocsai.local.bank
// @version      2.4.0
// @description  在「已批阅 / 查看答案」页面抓取题目与正确答案存入浏览器本地题库；答题前可一键切换 AI优先 / 本地+AI兜底 / 仅本地
// @author       local
// @match        *://*.chaoxing.com/*
// @match        *://*.edu.cn/*
// @match        *://*.org.cn/*
// @match        *://*.xueyinonline.com/*
// @match        *://*.hnsyu.net/*
// @match        *://*.qutjxjy.cn/*
// @match        *://*.ynny.cn/*
// @match        *://*.hnvist.cn/*
// @match        *://*.zhihuishu.com/*
// @match        *://*.icve.com.cn/*
// @match        *://*.yuketang.cn/*
// @match        *://*.icourse163.org/*
// @match        *://*.xuetangx.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  /* ------------------------------------------------------------------
     存储说明
     题库主库存在「脚本猫」的脚本存储里（= 浏览器本地）。
     本地服务只做镜像：每次打开页面 / 抓完题都会把整库推给服务端，
     由服务端负责「完全匹配 + 选项顺序换算」，脚本这边不重复实现匹配。
     换电脑就把 JSON 导出来带走。
     ------------------------------------------------------------------ */

  var API = 'http://127.0.0.1:8321';
  var BANK_KEY = 'ocsai_bank_v1';
  var P = 'ocb';           // class 前缀
  var MODES = [
    { k: 'ai_only',   n: 'AI 优先',   d: '全部交给 AI，不查本地题库' },
    { k: 'hybrid',    n: '本地+AI',   d: '本地完全匹配就用，没有就转 AI' },
    { k: 'bank_only', n: '仅本地',    d: '只用本地题库，没有就留空' }
  ];

  var state = { bank: [], mode: 'ai_only', open: false, busy: false, lastMiss: null };

  // ---------------- 基础
  function api(method, path, body) {
    return new Promise(function (resolve, reject) {
      GM_xmlhttpRequest({
        method: method,
        url: API + path,
        headers: { 'Content-Type': 'application/json' },
        data: body ? JSON.stringify(body) : null,
        timeout: 25000,
        onload: function (r) {
          try { resolve(JSON.parse(r.responseText)); }
          catch (e) { reject(new Error('返回异常：' + String(r.responseText).slice(0, 80))); }
        },
        onerror: function () { reject(new Error('本地服务没开：' + API)); },
        ontimeout: function () { reject(new Error('请求超时')); }
      });
    });
  }

  function loadBank() {
    try {
      var raw = GM_getValue(BANK_KEY, '[]');
      var arr = JSON.parse(raw || '[]');
      if (!Array.isArray(arr)) return [];
      // 清理历史脏数据：答案只有字母、却没抓到选项的条目。
      // 这种条目在题目换选项顺序时必然填错位置，留着只会添乱。
      var kept = arr.filter(function (e) {
        var a = String((e && e.answer) || '').replace(/\s+/g, '');
        var op = (e && e.options) || [];
        return !(/^[A-H](#[A-H])*$/i.test(a) && op.length < 2);
      });
      if (kept.length !== arr.length) {
        try { GM_setValue(BANK_KEY, JSON.stringify(kept)); } catch (e2) {}
      }
      return kept;
    } catch (e) { return []; }
  }

  function saveBank(arr) {
    state.bank = arr || [];
    try { GM_setValue(BANK_KEY, JSON.stringify(state.bank)); } catch (e) {
      toast('题库太大，浏览器存不下，先导出备份并清空一部分', true);
    }
  }

  function syncServer() {
    return api('POST', '/bank/sync', { items: state.bank }).catch(function () { return null; });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function normTitle(t) {
    return String(t || '').replace(/\s+/g, '').replace(/[，。、；：,.;:!?！？“”"'（）()\[\]【】<>《》\-—_/\\]/g, '');
  }

  // ---------------- 样式
  var CSS = ''
    + '.' + P + '-pill{position:fixed;left:16px;bottom:92px;z-index:2147483000;background:#1a1a1a;'
    + 'color:#fff;border-radius:999px;padding:8px 14px;font-size:12.5px;cursor:pointer;'
    + 'font-family:system-ui,"Microsoft YaHei",sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.24);'
    + 'opacity:.9;user-select:none;line-height:1.4;border:none}'
    + '.' + P + '-pill:hover{opacity:1}'
    + '.' + P + '-pill b{color:#7ee2b8}'
    + '.' + P + '-card{position:fixed;left:16px;bottom:92px;z-index:2147483001;width:268px;'
    + 'background:#fff;color:#1a1a1a;border-radius:14px;overflow:hidden;'
    + 'box-shadow:0 12px 44px rgba(0,0,0,.26);font-family:system-ui,"Microsoft YaHei",sans-serif;'
    + 'font-size:12.5px;line-height:1.55}'
    + '.' + P + '-hd{display:flex;align-items:center;justify-content:space-between;'
    + 'padding:11px 14px;background:#1a1a1a;color:#fff;font-size:13px;font-weight:600;cursor:pointer}'
    + '.' + P + '-hd span{font-weight:400;opacity:.72;font-size:11.5px}'
    + '.' + P + '-sec{padding:11px 14px;border-bottom:1px solid #f2f2f2}'
    + '.' + P + '-sec:last-child{border-bottom:none}'
    + '.' + P + '-lbl{color:#888;font-size:11px;margin-bottom:6px;letter-spacing:.02em}'
    + '.' + P + '-row{display:flex;gap:6px;flex-wrap:wrap}'
    + '.' + P + '-row button{flex:1;min-width:0;border:1px solid #dcdcdc;background:#fff;'
    + 'color:#333;border-radius:7px;padding:6px 4px;font-size:12px;cursor:pointer;'
    + 'font-family:inherit;white-space:nowrap}'
    + '.' + P + '-row button.on{background:#1a1a1a;color:#fff;border-color:#1a1a1a}'
    + '.' + P + '-act{display:flex;gap:6px}'
    + '.' + P + '-act button{flex:1;border:none;border-radius:7px;padding:8px 4px;font-size:12.5px;'
    + 'cursor:pointer;font-family:inherit;background:#f3f3f3;color:#333}'
    + '.' + P + '-act button.main{background:#0f7b4f;color:#fff}'
    + '.' + P + '-act button:hover{filter:brightness(.96)}'
    + '.' + P + '-meta{color:#999;font-size:11px;margin-top:7px}'
    + '.' + P + '-toast{position:fixed;left:50%;bottom:44px;transform:translateX(-50%);'
    + 'z-index:2147483002;background:#0f7b4f;color:#fff;border-radius:9px;padding:9px 16px;'
    + 'font-size:12.5px;font-family:system-ui,"Microsoft YaHei",sans-serif;pointer-events:none;'
    + 'box-shadow:0 6px 22px rgba(0,0,0,.24);max-width:70vw}'
    + '.' + P + '-toast.err{background:#c0392b}'
    + '.' + P + '-mask{position:fixed;inset:0;background:rgba(0,0,0,.42);z-index:2147483003;'
    + 'display:flex;align-items:center;justify-content:center}'
    + '.' + P + '-dlg{background:#fff;color:#1a1a1a;width:min(640px,92vw);max-height:80vh;'
    + 'border-radius:14px;display:flex;flex-direction:column;overflow:hidden;'
    + 'font-family:system-ui,"Microsoft YaHei",sans-serif;box-shadow:0 20px 60px rgba(0,0,0,.3)}'
    + '.' + P + '-dh{padding:15px 20px 9px;font-size:15px;font-weight:600}'
    + '.' + P + '-dh small{display:block;font-weight:400;color:#666;font-size:12px;margin-top:4px}'
    + '.' + P + '-db{overflow:auto;padding:0 20px 10px;flex:1;font-size:12.5px;line-height:1.6}'
    + '.' + P + '-it{padding:9px 0;border-bottom:1px solid #f2f2f2;word-break:break-all}'
    + '.' + P + '-it .o{margin:1px 0 1px 10px;color:#666;font-size:12px}'
    + '.' + P + '-it .o.hit{color:#0f7b4f;font-weight:600}'
    + '.' + P + '-it .a{color:#0f7b4f;font-weight:600;margin-top:3px}'
    + '.' + P + '-df{padding:12px 20px 15px;display:flex;gap:9px;justify-content:flex-end;'
    + 'border-top:1px solid #f0f0f0}'
    + '.' + P + '-df button{border:none;border-radius:8px;padding:9px 18px;font-size:13.5px;'
    + 'cursor:pointer;font-family:inherit}'
    + '.' + P + '-df .ok{background:#1a1a1a;color:#fff}'
    + '.' + P + '-df .no{background:#f2f2f2;color:#333}';

  function injectCss() {
    if (document.getElementById(P + '-style')) return;
    var s = document.createElement('style');
    s.id = P + '-style';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  function toast(msg, isErr) {
    var el = document.createElement('div');
    el.className = P + '-toast' + (isErr ? ' err' : '');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 2800);
  }

  // ---------------- 抓取：以每处「正确答案」为锚点切块，不依赖任何平台的 DOM 结构
  // 旧版做法是「先找一个题目容器、再从容器里抠一道题」，超星这种页面容器常常是
  // 包住整卷的外层节点，于是 65 道题被当成 1 道题、页眉也混进题干里。
  // 现在反着来：页面出现多少次「正确答案：」，就应该有多少道题，逐处切块解析。
  var ANS_ANCHOR = /(?:正确答案|参考答案|标准答案)\s*[:：]/;
  var NOISE_RE = /^(?:我的答案|我的回答|本题得分|得分|知识点|智能分析|答案解析|试题解析|解析|批语|老师评语|评分|收藏|纠错|试题纠错|重做|AI ?讲解|查看解析|展开|收起|共\s*\d+\s*题|题量|满分|作答时间|考试时间|剩余时间|正确率|答题卡|未作答|已作答)/;
  var QSTART_RE = /^\s*(?:第\s*\d{1,3}\s*题\s*[.、．:：]?|\d{1,3}\s*[.、．\)）]|[（(]\s*\d{1,3}\s*[）)])\s*/;
  var OPT_HEAD_RE = /^\s*([A-Za-z])\s*(?:[.．、\)）:：]\s*(.*))?$/;
  var TYPE_TAG_RE = /[【\[（(]\s*(单选题?|多选题?|判断题?|填空题?|简答题?|论述题?|不定项选择题?)\s*[】\]）)]/;
  var JUDGE_RE = /^(?:正确|错误|对|错|√|×|是|否)$/;

  function pageLines() {
    var t = document.body ? (document.body.innerText || '') : '';
    if (t.length > 800000) t = t.slice(0, 800000);
    return t.replace(/\u00a0/g, ' ').split('\n').map(function (s) { return s.trim(); });
  }

  function cleanAnswer(raw) {
    var a = String(raw || '').split(/\s{2,}/)[0] || '';
    a = a.replace(/(?:我的答案|本题得分|得分|解析|答案解析|知识点).*$/, '').trim();
    if (!a) return '';
    if (JUDGE_RE.test(a)) return a;
    // 多选题答案有三种常见排法：连写 ACD、空格 A C D、分隔符 A、C、D。
    // 旧写法要求「字母 + 分隔符 + 字母」，连写和纯空格的会被截成第一个字母，
    // 于是「ACD」入库成「A」——这正是 11-17 那三道多选错题的根因。
    var m = a.match(/^([A-Ha-h](?:[\s,，、#|\/]*[A-Ha-h])*)/);
    if (m) {
      var s = m[1].replace(/[^A-Ha-h]/g, '').toUpperCase();
      var seen = {}, uniq = [], i;
      for (i = 0; i < s.length; i++) {
        if (!seen[s[i]]) { seen[s[i]] = 1; uniq.push(s[i]); }
      }
      if (uniq.length) return uniq.join('#');
    }
    return a.slice(0, 80);
  }

  // 按答案锚点把整页切成「一题一块」
  function splitBlocks() {
    var lines = pageLines(), idx = [], i;
    for (i = 0; i < lines.length; i++) {
      if (ANS_ANCHOR.test(lines[i])) idx.push(i);
    }
    var blocks = [], prev = -1;
    idx.forEach(function (ii) {
      var raw = lines[ii].split(ANS_ANCHOR)[1] || '';
      var end = ii;
      // 有些平台把答案字母换行渲染：正确答案：/ A / C / D 各占一行。
      // 把紧跟其后的纯字母行并进来，并把游标推到这些行之后，避免污染下一题的选项。
      if (/^[A-Ha-h\s,，、#|\/]*$/.test(raw)) {
        var k2 = ii + 1, got = 0;
        while (k2 < lines.length && k2 <= ii + 6 && got < 8) {
          var nx = lines[k2];
          if (!nx) { k2++; continue; }
          // 只认「纯字母行」，带点的（A.）是选项行，不能当答案
          if (/^[A-Ha-h\s,，、#|\/]+$/.test(nx)) { raw += nx; end = k2; k2++; got++; }
          else break;
        }
      }
      var seg = [];
      for (var k = Math.max(prev + 1, 0); k < ii; k++) {
        var ln = lines[k];
        if (!ln || NOISE_RE.test(ln)) continue;
        seg.push(ln);
      }
      prev = end;
      var answer = cleanAnswer(raw);
      if (!answer || !seg.length) return;
      blocks.push({ lines: seg, answer: answer });
    });
    return { blocks: blocks, anchors: idx.length };
  }

  // 选项和题干挤在一行（如「（ ）A. Word B. Linux C. 记事本」）时拆出来
  function inlineOptions(text) {
    var re = /([A-H])\s*[.．、\)）:：]\s*/g, m, hits = [];
    while ((m = re.exec(text)) !== null) {
      hits.push({ letter: m[1], idx: m.index, end: re.lastIndex });
    }
    var best = null, bestLen = 1;
    for (var i = 0; i < hits.length; i++) {
      if (hits[i].letter !== 'A') continue;
      var seq = [hits[i]], expect = 66;
      for (var j = i + 1; j < hits.length; j++) {
        var L = hits[j].letter.charCodeAt(0);
        if (L === expect) { seq.push(hits[j]); expect = L + 1; }
        else if (L > expect) break;
      }
      if (seq.length > bestLen) { bestLen = seq.length; best = seq; }
    }
    if (!best || best.length < 2) return null;
    var opts = best.map(function (h, k) {
      var to = (k + 1 < best.length) ? best[k + 1].idx : text.length;
      return [h.letter, text.slice(h.end, to).replace(/[。；;，,、\s]+$/, '').trim()];
    }).filter(function (o) { return o[1]; });
    if (opts.length < 2) return null;
    return { stem: text.slice(0, best[0].idx).trim(), options: opts };
  }

  function parseBlock(seg, answer) {
    // 一个块里出现两个及以上题号，说明前面那道题没有「正确答案」（页面上没给），
    // 绝不能让它借用下一题的答案 —— 直接截断到最后一个题号开始的位置
    var lastQ = -1;
    for (var q = 0; q < seg.length; q++) if (QSTART_RE.test(seg[q])) lastQ = q;
    if (lastQ > 0) seg = seg.slice(lastQ);

    var i, m, aIdx = -1;
    for (i = 0; i < seg.length; i++) {
      m = seg[i].match(OPT_HEAD_RE);
      if (m && m[1].toUpperCase() === 'A') { aIdx = i; break; }
    }

    // 选项：从 A 开始按字母顺序连续读取；字母独占一行时把下一行当文字
    var opts = [];
    if (aIdx >= 0) {
      var expect = 65;
      for (var k = aIdx; k < seg.length && opts.length < 12; k++) {
        var mm = seg[k].match(OPT_HEAD_RE);
        if (!mm) {
          if (opts.length && !opts[opts.length - 1][1]) { opts[opts.length - 1][1] = seg[k]; continue; }
          break;
        }
        var L = mm[1].toUpperCase().charCodeAt(0);
        if (L !== expect) break;
        opts.push([String.fromCharCode(L), (mm[2] || '').trim()]);
        expect = L + 1;
      }
      opts = opts.filter(function (o) { return o[1]; });
      if (opts.length < 2) { opts = []; aIdx = -1; }
    }

    var stemLines = aIdx >= 0 ? seg.slice(0, aIdx) : seg.slice();
    var start = 0;
    for (var s = stemLines.length - 1; s >= 0; s--) {
      if (QSTART_RE.test(stemLines[s])) { start = s; break; }
    }
    var stem = stemLines.slice(start).join(' ').replace(/\s+/g, ' ').trim();
    var tm = stem.match(TYPE_TAG_RE);
    var qtype = tm ? tm[1] : '';
    stem = stem.replace(QSTART_RE, '').replace(TYPE_TAG_RE, '')
      .replace(/^[、.．\)）:：\s]+/, '').trim();

    if (!opts.length) {
      var io = inlineOptions(stem);
      if (io && io.stem.length >= 4) { opts = io.options; stem = io.stem; }
    }

    if (stem.length < 4) return null;
    if (stem.length > 600) return null;   // 明显是页眉/整段说明混进来了，宁可不要

    return { title: stem, type: qtype, options: opts, answer: answer, source: 'official' };
  }

  function collectStructured() {
    var r = splitBlocks(), out = [], seen = {}, dropped = 0, dup = 0, drops = [];
    r.blocks.forEach(function (b) {
      var it = parseBlock(b.lines, b.answer);
      if (!it) {
        dropped++;
        if (drops.length < 5) drops.push(String(b.lines[0] || '').slice(0, 30));
        return;
      }
      var key = normTitle(it.title);
      if (key.length < 4) { dropped++; return; }
      // 答案只有字母、却一个选项都没抓到 —— 这种条目以后无法按选项文字换算，
      // 存进去只会在换顺序时填错位置。宁可丢弃并报到「未解析」里。
      if (!it.options.length && /^[A-H](\s*#\s*[A-H])*$/i.test(String(it.answer || '').trim())) {
        dropped++;
        if (drops.length < 5) drops.push('缺选项：' + String(it.title || '').slice(0, 26));
        return;
      }
      if (seen[key]) { dup++; return; }
      seen[key] = 1;
      out.push(it);
    });
    return { items: out, anchors: r.anchors, dropped: dropped, dup: dup, drops: drops };
  }

  // 把本地解析结果拼回标准文本，交给服务端统一做题型推断/答案规整
  function itemsToText(items) {
    return items.map(function (it, i) {
      var s = (i + 1) + '. ' + (it.type ? '【' + it.type + '】' : '') + it.title;
      if (it.options && it.options.length) {
        s += '\n' + it.options.map(function (o) { return o[0] + '. ' + o[1]; }).join('\n');
      }
      return s + '\n正确答案：' + it.answer;
    }).join('\n\n');
  }

  // 长卷子多是懒加载，先自动滚到底把题目都渲出来再抓
  function autoScroll() {
    return new Promise(function (resolve) {
      var t0 = Date.now(), stall = 0, last = -1;
      var timer = setInterval(function () {
        var h = Math.max(document.documentElement.scrollHeight || 0, document.body.scrollHeight || 0);
        window.scrollBy(0, Math.round(window.innerHeight * 0.9));
        var y = window.scrollY || document.documentElement.scrollTop || 0;
        if (y + window.innerHeight >= h - 8) stall++;
        if (y === last) stall++;
        last = y;
        if (stall >= 2 || Date.now() - t0 > 20000) {
          clearInterval(timer);
          window.scrollTo(0, 0);
          setTimeout(resolve, 350);
        }
      }, 260);
    });
  }

  // ---------------- 预览 + 入库
  function preview(items, diag) {
    diag = diag || {};
    var mask = document.createElement('div');
    mask.className = P + '-mask';

    // 每道题列出全部选项、把「正确答案」对应的那一项标绿，方便肉眼核对
    function itemHtml(it) {
      var letters = String(it.answer || '').replace(/[^A-Ha-h]/g, '').toUpperCase().split('');
      var opts = (it.options || []).map(function (o) {
        var hit = letters.indexOf(o[0]) >= 0;
        return '<div class="' + P + '-o' + (hit ? ' hit' : '') + '">'
          + esc(o[0]) + '. ' + esc(o[1]) + '</div>';
      }).join('');
      var ans = String(it.answer);
      if (letters.length && it.options && it.options.length) {
        var txts = it.options.filter(function (o) { return letters.indexOf(o[0]) >= 0; })
          .map(function (o) { return o[1]; });
        if (txts.length) ans += '  ' + txts.join(' / ');
      }
      return '<div class="' + P + '-it"><div>' + esc(it.title) + '</div>'
        + opts + '<div class="a">正确答案：' + esc(ans) + '</div></div>';
    }

    var html = items.slice(0, 200).map(itemHtml).join('');
    if (items.length > 200) {
      html += '<div class="' + P + '-it" style="color:#999">… 还有 ' + (items.length - 200) + ' 道未显示</div>';
    }

    var noOpt = items.filter(function (it) {
      return /单|多/.test(it.type || '') && (!it.options || it.options.length < 2);
    }).length;
    // 多选答案只剩一个字母 = 大概率被截断了（或这题真的只有一个正确项），单独提示便于核对
    var oneLetter = items.filter(function (it) {
      return /多/.test(it.type || '') && it.answer && it.answer.indexOf('#') < 0;
    }).length;

    var parts = [];
    if (diag.anchors) parts.push('页面答案锚点 ' + diag.anchors + ' 处');
    parts.push('解析出 ' + items.length + ' 道');
    if (diag.dup) parts.push('题干重复 ' + diag.dup + ' 处（合并）');
    if (diag.dropped) parts.push('未解析 ' + diag.dropped + ' 处');
    if (noOpt) parts.push('缺选项 ' + noOpt + ' 道');
    if (oneLetter) parts.push('多选仅 1 个字母 ' + oneLetter + ' 道（请核对是否被截断）');
    var sub = parts.join(' ｜ ') + '。答案只取「正确答案」后面的内容，选项按 A/B/C 全部收录。';

    var dropNote = '';
    if (diag.drops && diag.drops.length) {
      dropNote = '<div style="color:#b06a00;background:#fff8ec;border-radius:8px;'
        + 'padding:7px 10px;margin-bottom:6px;font-size:12px">这几处没解析出题干（题干过长或排版特殊）：'
        + diag.drops.map(esc).join(' ／ ') + '</div>';
    }

    mask.innerHTML = '<div class="' + P + '-dlg">'
      + '<div class="' + P + '-dh">抓到 ' + items.length + ' 道带答案的题'
      + '<small>' + sub + '</small></div>'
      + '<div class="' + P + '-db">' + dropNote + html + '</div>'
      + '<div class="' + P + '-df"><button class="no">取消</button>'
      + '<button class="ok">确认入库</button></div></div>';

    mask.querySelector('.no').onclick = function () { mask.remove(); };
    mask.querySelector('.ok').onclick = function () {
      var btn = mask.querySelector('.ok');
      btn.disabled = true; btn.textContent = '写入中…';

      var map = {};
      state.bank.forEach(function (e, i) { map[normTitle(e.title)] = i; });
      var added = 0, replaced = 0;
      items.forEach(function (it) {
        var raw = String(it.answer || '');
        var m = raw.match(/(?:正确答案|参考答案|标准答案)\s*[:：]\s*([\s\S]+)$/);
        var ans = (m ? m[1] : raw).trim();
        if (!it.title || !ans) return;
        var key = normTitle(it.title);
        var rec = {
          title: it.title, type: it.type || '', options: it.options || [],
          answer: ans, source: 'official', added: Date.now()
        };
        if (map[key] !== undefined) { state.bank[map[key]] = rec; replaced++; }
        else { map[key] = state.bank.length; state.bank.push(rec); added++; }
      });
      saveBank(state.bank);

      // 浏览器是主库，整库推给服务端做镜像（服务端按完全匹配 + 选项换算出答案）
      syncServer()
        .then(function () {
          mask.remove();
          render();
          toast('新增 ' + added + ' 道' + (replaced ? ('，更新 ' + replaced + ' 道') : '') + '，已同步');
        })
        .catch(function (e) {
          mask.remove();
          render();
          toast('已存浏览器，但服务端没同步：' + e.message, true);
        });
    };
    document.body.appendChild(mask);
  }

  function doCapture() {
    if (state.busy) return;
    state.busy = true;
    render();
    toast('先滚一遍页面把题目都加载出来…');
    autoScroll().then(function () {
      var r;
      try { r = collectStructured(); } catch (e) { r = { items: [], anchors: 0 }; }
      if (!r.items.length) {
        state.busy = false; render();
        toast('这页没找到带「正确答案」的题：先交卷，进「查看答案 / 已批阅」页再抓', true);
        return;
      }
      return api('POST', '/bank/import', { text: itemsToText(r.items), dry_run: true })
        .then(function (d) {
          var srv = (d && d.items) || [];
          // 服务端只负责把题型/答案规整成标准格式；数量对不上说明它切错了，用本地结果
          var list = (srv.length === r.items.length) ? srv : r.items;
          state.busy = false; render();
          preview(list, r);
        })
        .catch(function () {
          state.busy = false; render();
          preview(r.items, r);
        });
    });
  }

  // ---------------- 面板
  function render() {
    injectCss();
    var old = document.querySelector('.' + P + '-pill, .' + P + '-card');
    var wasOpen = state.open;
    if (old) old.remove();

    if (!wasOpen) {
      var pill = document.createElement('button');
      pill.className = P + '-pill';
      pill.type = 'button';
      pill.innerHTML = '题库 <b>' + state.bank.length + '</b> · ' + modeName(state.mode);
      pill.onclick = function () { state.open = true; render(); };
      document.body.appendChild(pill);
      return;
    }

    var card = document.createElement('div');
    card.className = P + '-card';
    var modeBtns = MODES.map(function (m) {
      return '<button data-m="' + m.k + '" class="' + (m.k === state.mode ? 'on' : '') + '">' + m.n + '</button>';
    }).join('');

    card.innerHTML = ''
      + '<div class="' + P + '-hd">题库助手 <span>点这里收起</span></div>'
      + '<div class="' + P + '-sec"><div class="' + P + '-lbl">答题路线（答题前选）</div>'
      + '<div class="' + P + '-row" id="' + P + '-modes">' + modeBtns + '</div>'
      + '<div class="' + P + '-meta" id="' + P + '-mdesc"></div></div>'
      + '<div class="' + P + '-sec"><div class="' + P + '-lbl">题库</div>'
      + '<div class="' + P + '-act">'
      + '<button class="main" id="' + P + '-cap">' + (state.busy ? '抓取中…' : '抓本页答案') + '</button>'
      + '</div>'
      + '<div class="' + P + '-meta">浏览器本地存了 <b>' + state.bank.length + '</b> 道题</div>'
      + '<div class="' + P + '-act" style="margin-top:7px">'
      + '<button id="' + P + '-exp">导出</button>'
      + '<button id="' + P + '-imp">导入</button>'
      + '<button id="' + P + '-clr">清空</button>'
      + '</div></div>';

    card.querySelector('.' + P + '-hd').onclick = function () { state.open = false; render(); };
    card.querySelector('#' + P + '-cap').onclick = doCapture;

    card.querySelectorAll('#' + P + '-modes button').forEach(function (b) {
      b.onclick = function () { setMode(b.getAttribute('data-m')); };
    });

    card.querySelector('#' + P + '-exp').onclick = function () {
      var blob = new Blob([JSON.stringify(state.bank, null, 2)], { type: 'application/json' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'OCS题库_' + new Date().toISOString().slice(0, 10) + '.json';
      a.click();
    };

    card.querySelector('#' + P + '-imp').onclick = function () {
      var inp = document.createElement('input');
      inp.type = 'file'; inp.accept = '.json,application/json';
      inp.onchange = function () {
        var f = inp.files && inp.files[0];
        if (!f) return;
        var fr = new FileReader();
        fr.onload = function () {
          try {
            var arr = JSON.parse(String(fr.result));
            if (!Array.isArray(arr)) throw new Error('不是题库数组');
            var map = {};
            state.bank.forEach(function (e, i) { map[normTitle(e.title)] = i; });
            var add = 0;
            arr.forEach(function (it) {
              if (!it || !it.title || !it.answer) return;
              var key = normTitle(it.title);
              if (map[key] !== undefined) state.bank[map[key]] = it;
              else { map[key] = state.bank.length; state.bank.push(it); add++; }
            });
            saveBank(state.bank);
            syncServer().then(function () { render(); toast('导入完成，新增 ' + add + ' 道'); });
          } catch (e) { toast('导入失败：' + e.message, true); }
        };
        fr.readAsText(f, 'utf-8');
      };
      inp.click();
    };

    card.querySelector('#' + P + '-clr').onclick = function () {
      if (!confirm('清空浏览器里的 ' + state.bank.length + ' 道题？建议先导出备份。')) return;
      saveBank([]);
      syncServer().then(function () { render(); toast('已清空'); });
    };

    document.body.appendChild(card);
    updateModeDesc(card);
  }

  function updateModeDesc(card) {
    var el = card.querySelector('#' + P + '-mdesc');
    if (!el) return;
    var m = MODES.filter(function (x) { return x.k === state.mode; })[0];
    el.textContent = m ? m.d : '';
  }

  function modeName(k) {
    var m = MODES.filter(function (x) { return x.k === k; })[0];
    return m ? m.n : k;
  }

  function setMode(k) {
    state.mode = k;
    render();
    api('POST', '/route', { mode: k })
      .then(function () { toast('路线已切到「' + modeName(k) + '」'); })
      .catch(function (e) { toast('切换失败：' + e.message, true); });
  }

  // ---------------- 答题时提示「本地有没有这题」
  function watchMiss() {
    setInterval(function () {
      api('GET', '/stats').then(function (d) {
        var s = (d && d.stats) || {};
        if (state.lastMiss === null) { state.lastMiss = s.bank_miss || 0; return; }
        var now = s.bank_miss || 0;
        if (now > state.lastMiss) {
          toast(state.mode === 'bank_only'
            ? '本题本地题库没有，已跳过（仅本地模式，未调用 AI）'
            : '本题本地题库没有，已转 AI 作答');
        }
        state.lastMiss = now;
      }).catch(function () {});
    }, 4000);
  }

  // ---------------- 启动
  function boot() {
    state.bank = loadBank();
    injectCss();
    render();
    syncServer();
    api('GET', '/route').then(function (d) {
      if (d && d.mode) { state.mode = d.mode; render(); }
    }).catch(function () {});
    watchMiss();
    setInterval(function () {
      if (!document.querySelector('.' + P + '-pill, .' + P + '-card')) render();
    }, 4000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
