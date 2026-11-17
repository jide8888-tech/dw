/**
 * 화물차 운행일지 — 구글폼 + 구글시트 자동 생성 스크립트
 *
 * 사용법
 *  1) https://script.google.com 접속 → [새 프로젝트]
 *  2) 이 파일 내용을 전부 붙여넣기
 *  3) 아래 VEHICLES 에 차량번호(또는 기사명)를 적기
 *  4) 위쪽 함수 선택에서 setup 선택 → [실행] → 권한 허용
 *  5) [실행 로그]에 나오는 시트 주소를 열고 '기사별 링크' 탭의 링크를 카톡으로 전달
 *
 * 차량이 늘거나 줄면: VEHICLES 수정 → updateVehicles 실행
 */

// ===== 여기만 수정하세요 =====
const VEHICLES = ['12가3456', '34나5678', '56다7890'];
const TITLE = '화물차 운행일지';
// ============================

const RESP_SHEET = '응답원본';
const LINK_SHEET = '기사별 링크';
const HEADERS = ['일자', '출발km', '도착km', '총 주행km', '주유량(L)', '주유금액(원)'];

function setup() {
  const props = PropertiesService.getScriptProperties();
  if (props.getProperty('formId')) {
    throw new Error('이미 만들어져 있습니다. 차량 변경은 updateVehicles 를 실행하세요.');
  }

  const ss = SpreadsheetApp.create(TITLE);
  const form = FormApp.create(TITLE);
  form.setDescription('출발할 때 한 번, 운행이 끝나면 한 번 입력해 주세요.')
      .setConfirmationMessage('입력 완료되었습니다. 안전운행 하세요!')
      .setCollectEmail(false)
      .setShowLinkToRespondAgain(false);

  // 1페이지: 차량 / 입력 시점
  const vehicleItem = form.addListItem().setTitle('차량번호').setRequired(true);
  vehicleItem.setChoiceValues(VEHICLES);
  const whenItem = form.addMultipleChoiceItem().setTitle('지금 입력하는 시점').setRequired(true);

  // 2페이지: 출발
  const startPage = form.addPageBreakItem().setTitle('출발 입력');
  form.addTextItem().setTitle('출발 km (계기판)').setRequired(true)
      .setValidation(FormApp.createTextValidation().requireNumber()
        .setHelpText('숫자만 입력하세요').build());

  // 3페이지: 운행 종료
  const endPage = form.addPageBreakItem().setTitle('운행 종료 입력');
  endPage.setGoToPage(FormApp.PageNavigationType.SUBMIT); // 출발 페이지 끝나면 바로 제출
  form.addTextItem().setTitle('도착 km (계기판)').setRequired(true)
      .setValidation(FormApp.createTextValidation().requireNumber()
        .setHelpText('숫자만 입력하세요').build());
  form.addTextItem().setTitle('주유량 (L)').setHelpText('주유 안 했으면 비워두세요')
      .setValidation(FormApp.createTextValidation().requireNumber()
        .setHelpText('숫자만 입력하세요').build());
  form.addTextItem().setTitle('주유금액 (원)').setHelpText('주유 안 했으면 비워두세요')
      .setValidation(FormApp.createTextValidation().requireWholeNumber()
        .setHelpText('숫자만 입력하세요 (쉼표 없이)').build());

  whenItem.setChoices([
    whenItem.createChoice('출발', startPage),
    whenItem.createChoice('운행 종료', endPage),
  ]);

  // 폼 → 시트 연결
  form.setDestination(FormApp.DestinationType.SPREADSHEET, ss.getId());
  SpreadsheetApp.flush();
  Utilities.sleep(3000);
  const linked = SpreadsheetApp.openById(ss.getId()).getSheets()
      .find(s => s.getFormUrl && s.getFormUrl());
  linked.setName(RESP_SHEET);

  props.setProperties({ formId: form.getId(), ssId: ss.getId(), vehicleItemId: String(vehicleItem.getId()) });

  buildVehicleTabs_(ss, form);

  // 기본으로 생기는 빈 시트 삭제
  const blank = ss.getSheetByName('Sheet1') || ss.getSheetByName('시트1');
  if (blank && ss.getSheets().length > 1) ss.deleteSheet(blank);

  Logger.log('시트: ' + ss.getUrl());
  Logger.log('폼(편집): ' + form.getEditUrl());
  Logger.log('폼(입력): ' + form.getPublishedUrl());
}

function updateVehicles() {
  const props = PropertiesService.getScriptProperties();
  const form = FormApp.openById(props.getProperty('formId'));
  const ss = SpreadsheetApp.openById(props.getProperty('ssId'));
  form.getItemById(Number(props.getProperty('vehicleItemId'))).asListItem().setChoiceValues(VEHICLES);
  buildVehicleTabs_(ss, form);
  Logger.log('업데이트 완료: ' + ss.getUrl());
}

/** 차량별 탭(일자/출발km/도착km/총 주행km/주유량/주유금액) + 기사별 링크 탭 */
function buildVehicleTabs_(ss, form) {
  const R = "'" + RESP_SHEET + "'!";
  // 응답원본 열: A 타임스탬프, B 차량번호, C 시점, D 출발km, E 도착km, F 주유량, G 주유금액
  const pick = col =>
    '=MAP(A2:A, LAMBDA(d, IF(d="", "", IFERROR(CHOOSEROWS(FILTER(' +
    R + col + '2:' + col + ', ' + R + 'B2:B=$H$1, INT(' + R + 'A2:A)=d, ' +
    R + col + '2:' + col + '<>""), -1), ""))))';

  VEHICLES.forEach(v => {
    if (ss.getSheetByName(v)) return;
    const sh = ss.insertSheet(v);
    sh.getRange(1, 1, 1, 6).setValues([HEADERS]).setFontWeight('bold').setBackground('#e8eef7');
    sh.getRange('H1').setValue(v).setFontColor('#999999'); // 이 탭의 차량번호 (수식에서 사용)
    sh.getRange('A2').setFormula(
      '=IFERROR(SORT(UNIQUE(FILTER(INT(' + R + 'A2:A), ' + R + 'B2:B=$H$1))), "")');
    sh.getRange('B2').setFormula(pick('D'));
    sh.getRange('C2').setFormula(pick('E'));
    sh.getRange('D2').setFormula(
      '=MAP(B2:B, C2:C, LAMBDA(s, e, IF(OR(s="", e=""), "", e - s)))');
    sh.getRange('E2').setFormula(pick('F'));
    sh.getRange('F2').setFormula(pick('G'));
    sh.getRange('A2:A').setNumberFormat('yyyy-mm-dd (ddd)');
    sh.getRange('B2:D').setNumberFormat('#,##0');
    sh.getRange('E2:E').setNumberFormat('#,##0.0');
    sh.getRange('F2:F').setNumberFormat('#,##0');
    sh.setFrozenRows(1);
    sh.setColumnWidths(1, 6, 110);
  });

  // 차량별 미리 채워진 링크 → 기사는 차량 선택도 안 해도 됨
  let ls = ss.getSheetByName(LINK_SHEET) || ss.insertSheet(LINK_SHEET, 0);
  ls.clear();
  const vItem = form.getItemById(
    Number(PropertiesService.getScriptProperties().getProperty('vehicleItemId'))).asListItem();
  const rows = VEHICLES.map(v => [v, form.createResponse()
      .withItemResponse(vItem.createResponse(v)).toPrefilledUrl()]);
  ls.getRange(1, 1, 1, 2).setValues([['차량번호', '카톡으로 보낼 링크']]).setFontWeight('bold');
  ls.getRange(2, 1, rows.length, 2).setValues(rows);
  ls.setColumnWidth(2, 600);
}
