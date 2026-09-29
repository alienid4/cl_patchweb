/* ============================================================
 * js/reminder.js
 * 產生可直接複製的催辦 Mail 文字。
 * ============================================================ */
(function (global) {
  'use strict';

  var U = global.Utils;
  var CFG = global.APP_CONFIG;
  var A = global.Analysis;

  /* ownerGroup = Analysis.byOwner 的單一元素 { owner, records, ... }
   * 催辦聚焦：只列「已逾期」與「快到期(soonDays 內未逾期)」；
   * 例外保護中且日期還遠的(安全名單)不打擾。 */
  function build(ownerGroup) {
    var owner = ownerGroup.owner;
    var soonDays = CFG.soonDays || 30;
    var recs = ownerGroup.records.slice();

    // 依真正到期日排序(逾期在前、越急越前)
    recs.sort(function (a, b) {
      var da = a.daysLeft === null ? 999999 : a.daysLeft;
      var db = b.daysLeft === null ? 999999 : b.daysLeft;
      return da - db;
    });

    var overdue = recs.filter(A.helpers.isOverdue);
    var soon = recs.filter(function (r) {
      return !r.overdue && A.helpers.withinDays(r, soonDays);
    });
    var focusCount = overdue.length + soon.length;

    var subject = CFG.mail.subjectPrefix + owner + ' 弱點修補（逾期 ' +
      overdue.length + ' 筆、' + soonDays + '天內到期 ' + soon.length + ' 筆）';

    var L = [];
    L.push(owner + ' 您好：');
    L.push('');

    function line(idx, r) {
      var status = r.overdue ? ('已逾期 ' + r.overdueDays + ' 天')
                 : (r.daysLeft === 0 ? '今日到期' : ('距到期 ' + r.daysLeft + ' 天'));
      var stageTag = r.stage === 'exception' ? '（例外管理中）'
                   : r.stage === 'extension' ? '（首次展延中）' : '';
      return idx + '. [' + (r.severity || '-') + '] ' + (r.name || '(無名稱)') +
             '\n     主機：' + (r.host || '-') +
             '　Plugin ID：' + (r.pluginId || '-') +
             '\n     真正到期日：' + U.fmtDate(r.realDue) + '（' + status + '）' + stageTag;
    }

    if (focusCount === 0) {
      L.push('依最新弱點掃描彙總，您名下（' + CFG.filter.department +
             '）目前無「已逾期」或「' + soonDays + ' 天內到期」的弱點，感謝配合。');
      L.push('');
      L.push(CFG.mail.signature);
      return { subject: CFG.mail.subjectPrefix + owner + ' 弱點修補（目前無急迫項目）', body: L.join('\n') };
    }

    L.push('依最新弱點掃描彙總，您名下（' + CFG.filter.department +
           '）有 ' + focusCount + ' 筆弱點需儘速處理（已逾期 ' + overdue.length +
           ' 筆、' + soonDays + ' 天內到期 ' + soon.length + ' 筆）。明細如下：');
    L.push('');

    var n = 1;
    if (overdue.length) {
      L.push('【已逾期，請立即處理】');
      overdue.forEach(function (r) { L.push(line(n++, r)); });
      L.push('');
    }
    if (soon.length) {
      L.push('【' + soonDays + ' 天內即將到期】');
      soon.forEach(function (r) { L.push(line(n++, r)); });
      L.push('');
    }

    L.push(CFG.mail.ccNote);
    L.push('');
    L.push(CFG.mail.signature);

    return { subject: subject, body: L.join('\n') };
  }

  /* 批次催辦：把所有「有急迫項目(逾期或快到期)」的負責人打包成一份文字。 */
  function buildAll(owners) {
    var soonDays = CFG.soonDays || 30;
    var blocks = [];
    var included = 0, skipped = 0;
    owners.forEach(function (o) {
      var overdue = o.overdue || 0;
      var soon = o.records.filter(function (r) {
        return !r.overdue && A.helpers.withinDays(r, soonDays);
      }).length;
      if (overdue + soon === 0) { skipped++; return; }
      included++;
      var m = build(o);
      blocks.push('═══════════════════════════════════════\n' +
                  '收件人：' + o.owner + '　|　主旨：' + m.subject + '\n' +
                  '───────────────────────────────────────\n' + m.body);
    });

    var header = '【批次催辦彙整】' + CFG.filter.department +
      '　需催辦 ' + included + ' 人（另 ' + skipped + ' 人無急迫項目，已略過）\n' +
      '產生方式：每位一段，可整段複製或分段貼入各自信件。\n';

    var subject = CFG.mail.subjectPrefix + CFG.filter.department + ' 批次催辦（' + included + ' 人）';
    var body = included ? (header + '\n' + blocks.join('\n\n'))
                        : (header + '\n目前所有負責人皆無「已逾期 / ' + soonDays + ' 天內到期」項目。');
    return { subject: subject, body: body, included: included, skipped: skipped };
  }

  global.Reminder = { build: build, buildAll: buildAll };
})(window);
