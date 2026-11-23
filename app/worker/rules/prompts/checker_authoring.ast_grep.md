할 일: 규칙을 어긴 Python 코드를 찾는 ast-grep 규칙(YAML 문서 하나)을 checker에 쓴다.
- 키: id, language(python), severity(error), message(규칙을 한 줄로), rule. 필요하면 constraints,
  utils(이 파일 안에 정의하고 matches로 참조), note.
- 쓰지 않는 키: fix, rewriters, transform, files, ignores. 적용 범위(경로)는 파이프라인이 정한다.
- YAML 별칭(&, *)을 쓰지 않는다.
- 규칙을 어긴 바로 그 노드(호출, import 문, 데코레이터, 리터럴, 문장)를 잡는다. 감싸는 함수나
  클래스 전체를 잡지 않는다. 잡힌 노드의 첫 줄이 위반 예시에 표시한 줄이어야 한다.
- 참고: 메타 변수는 $A, 여러 개는 $$$ARGS. tree-sitter-python의 kind 이름(call, attribute,
  import_statement, import_from_statement, dotted_name, decorator, decorated_definition,
  class_definition, function_definition, comparison_operator, binary_operator, integer, float,
  global_statement, assignment, keyword_argument)과 has, inside, not, any, all, regex,
  stopBy: end, field를 쓸 수 있다.
- ast-grep 규칙은 잡을 노드와 그 안팎의 노드 모양만 본다. 규칙이 덮는 모양 중 그것만으로 가릴 수
  없는 것이 있으면(이름이 파일의 어디에 정의됐는지, 앞에서 무엇을 대입했는지를 따져야 하는 경우
  등) 그 모양을 빼고 쓰지 않는다. 대신 unfit에 무엇을 가릴 수 없는지 한 줄로 적는다. 그러면 다음
  시도는 같은 규칙을 Python ast 스크립트로 쓴다. 모두 가릴 수 있으면 unfit은 빈 문자열이다.
- unfit은 ast-grep 형식의 한계이고 coverage와 따로다. Python ast 스크립트로 모두 가릴 수 있으면
  coverage는 all이다.
