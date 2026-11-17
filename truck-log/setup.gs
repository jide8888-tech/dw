/**
 * 화물차 운행일지 — 구글폼 + 구글시트 자동 생성 스크립트 (차량 1대)
 *
 * 사용법
 *  1) https://script.google.com 접속 → [새 프로젝트]
 *  2) 이 파일 내용을 전부 붙여넣기
 *  3) 위쪽 함수 선택에서 setup 선택 → [실행] → 권한 허용
 *  4) [실행 로그]에 나오는 '폼(입력)' 링크를 기사님께 카톡으로 전달
 *
 * 다른 관리자와 같이 보려면: EDITORS 에 구글 계정 적기 → share 실행
 * (기사님은 구글 계정 없이 링크만으로 입력 가능)
 */

// ===== 여기만 수정하세요 =====
const TITLE = '화물차 운행일지';
// 시트·폼을 같이 관리할 사람의 구글 계정 (예: ['manager@gmail.com'])
const EDITORS = [];
// ============================

const RESP_SHEET = '응답원본';
const LOG_SHEET = '운행일지';
const HEADERS = ['일자', '출발km', '도착km', '총 주행km', '주유량(L)', '주유금액(원)'];

function setup() {
  const props = PropertiesService.getScriptProperties();
  if (props.getProperty('formId')) {
    throw new Error('이미 만들어져 있습니다. 실행 로그 또는 구글 드라이브에서 확인하세요.');
  }

  const ss = SpreadsheetApp.create(TITLE);
  const form = FormApp.create(TITLE);
  form.setDescription('출발할 때 한 번, 운행이 끝나면 한 번 입력해 주세요.')
      .setConfirmationMessage('입력 완료되었습니다. 안전운행 하세요!')
      .setCollectEmail(false)
      .setShowLinkToRespondAgain(false);

  // 1페이지: 입력 시점
  const whenItem = form.addMultipleChoiceItem().setTitle('지금 입력하는 시점').setRequired(true);

  // 2페이지: 출발
  const startPage = form.addPageBreakItem().setTitle('출발 입력');
  form.addTextItem().setTitle('출발 km (계기판)').setRequired(true)
      .setValidation(numberRule_());

  // 3페이지: 운행 종료
  const endPage = form.addPageBreakItem().setTitle('운행 종료 입력');
  endPage.setGoToPage(FormApp.PageNavigationType.SUBMIT); // 출발 페이지 끝나면 바로 제출
  form.addTextItem().setTitle('도착 km (계기판)').setRequired(true)
      .setValidation(numberRule_());
  form.addTextItem().setTitle('주유량 (L)').setHelpText('주유 안 했으면 비워두세요')
      .setValidation(numberRule_());
  form.addTextItem().setTitle('주유금액 (원)').setHelpText('주유 안 했으면 비워두세요')
      .setValidation(FormApp.createTextValidation().requireWholeNumber()
        .setHelpText('숫자만 입력하세요 (쉼표 없이)').build());

  whenItem.setChoices([
    whenItem.createChoice('출발', startPage),
    whenItem.createChoice('운행 종료', endPage),
  ]);

  // 폼 → 시트 연결 (응답이 '응답원본' 탭에 쌓임)
  form.setDestination(FormApp.DestinationType.SPREADSHEET, ss.getId());
  SpreadsheetApp.flush();
  Utilities.sleep(3000);
  const book = SpreadsheetApp.openById(ss.getId());
  book.getSheets().find(s => s.getFormUrl()).setName(RESP_SHEET);

  buildLogSheet_(book);

  // 기본으로 생기는 빈 시트 삭제
  const blank = book.getSheetByName('Sheet1') || book.getSheetByName('시트1');
  if (blank) book.deleteSheet(blank);

  props.setProperties({ formId: form.getId(), ssId: ss.getId() });
  shareWithEditors_(book, form);

  Logger.log('시트: ' + ss.getUrl());
  Logger.log('폼(편집): ' + form.getEditUrl());
  Logger.log('폼(입력) ← 기사님께 보낼 링크: ' + form.getPublishedUrl());
}

/** 응답원본을 날짜별 한 줄로 정리하는 '운행일지' 탭 */
function buildLogSheet_(ss) {
  const R = "'" + RESP_SHEET + "'!";
  // 응답원본 열: A 타임스탬프, B 시점, C 출발km, D 도착km, E 주유량, F 주유금액
  // 같은 날 여러 번 보냈으면 마지막 값 사용
  const pick = col =>
    '=MAP(A2:A, LAMBDA(d, IF(d="", "", IFERROR(CHOOSEROWS(FILTER(' +
    R + col + '2:' + col + ', INT(' + R + 'A2:A)=d, ' +
    R + col + '2:' + col + '<>""), -1), ""))))';

  const sh = ss.insertSheet(LOG_SHEET, 0);
  sh.getRange(1, 1, 1, 6).setValues([HEADERS]).setFontWeight('bold').setBackground('#e8eef7');
  sh.getRange('A2').setFormula(
    '=IFERROR(SORT(UNIQUE(FILTER(INT(' + R + 'A2:A), ' + R + 'A2:A<>""))), "")');
  sh.getRange('B2').setFormula(pick('C'));
  sh.getRange('C2').setFormula(pick('D'));
  sh.getRange('D2').setFormula(
    '=MAP(B2:B, C2:C, LAMBDA(s, e, IF(OR(s="", e=""), "", e - s)))');
  sh.getRange('E2').setFormula(pick('E'));
  sh.getRange('F2').setFormula(pick('F'));
  sh.getRange('A2:A').setNumberFormat('yyyy-mm-dd (ddd)');
  sh.getRange('B2:D').setNumberFormat('#,##0');
  sh.getRange('E2:E').setNumberFormat('#,##0.0');
  sh.getRange('F2:F').setNumberFormat('#,##0');
  sh.setFrozenRows(1);
  sh.setColumnWidths(1, 6, 110);
}

/** EDITORS 수정 후 실행하면 그 계정들에 시트·폼 편집 권한을 줍니다 */
function share() {
  const props = PropertiesService.getScriptProperties();
  shareWithEditors_(SpreadsheetApp.openById(props.getProperty('ssId')),
                    FormApp.openById(props.getProperty('formId')));
}

function shareWithEditors_(ss, form) {
  const emails = EDITORS.map(e => e.trim()).filter(Boolean);
  if (!emails.length) return;
  ss.addEditors(emails);
  form.addEditors(emails);
  Logger.log('공유 완료: ' + emails.join(', '));
}

function numberRule_() {
  return FormApp.createTextValidation().requireNumber()
      .setHelpText('숫자만 입력하세요').build();
}
