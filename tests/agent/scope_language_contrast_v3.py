"""C2 v3 contrast cases for standing "X하지 말고 / without X" limits (written with the v3 change,
by the same author: NOT an independent or held-out set). True = should be recorded.

Axes: Korean/English x tool / server / period / count / format (one-off answer shaping).
"""

CONTRAST = [
    # period-bound tool limits -> standing
    ("이 대화 동안 웹 검색은 쓰지 말고 진행해.", True, "ko-period-tool"),
    ("앞으로 외부 API는 호출하지 말고 캐시만 써.", True, "ko-period-tool"),
    ("오늘은 브라우저 열지 말고 로그만 봐줘.", True, "ko-period-tool"),
    ("For the rest of this session, don't use web search; just work from the repo.", True, "en-period-tool"),
    ("During this task, work without the browser tool.", True, "en-period-tool"),
    # server / environment / repository actions -> standing
    ("운영 서버에는 접속하지 말고 로컬에서만 테스트해.", True, "ko-server"),
    ("프로덕션 DB는 건드리지 말고 스테이징만 써.", True, "ko-server"),
    ("커밋은 하지 말고 패치 파일로만 남겨줘.", True, "ko-repo"),
    ("배포하지 말고 빌드까지만 해.", True, "ko-repo"),
    ("Test locally instead of connecting to the production server.", True, "en-server"),
    ("Prepare the patch without pushing to the remote.", True, "en-repo"),
    # count limits -> standing
    ("재시도는 두 번까지만 하고 그 이상은 하지 말고 멈춰.", True, "ko-count"),
    ("Run the migration once only, not more than once.", True, "en-count"),
    # one-off answer shaping -> not recorded (the request was handled in its own turn)
    ("길게 쓰지 말고 세 줄로 요약해.", False, "ko-format"),
    ("표 만들지 말고 문장으로 정리해줘.", False, "ko-format"),
    ("도구를 사용하지 말고 한 문장으로 답해줘.", False, "ko-format-tool-oneoff"),
    ("검색하지 말고 아는 범위에서 답해줘.", False, "ko-format-tool-oneoff"),
    ("코드 블록 쓰지 말고 설명해줘.", False, "ko-format"),
    ("Summarize it without bullet points.", False, "en-format"),
    ("Answer in one line instead of a list.", False, "en-format"),
    ("Explain it without using any tools.", False, "en-format-tool-oneoff"),
]
