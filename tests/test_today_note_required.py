"""Today's note box is marked required for "Add the note" and "Done: I checked it", so an empty press is stopped by the browser before the
post, while Assign (which takes no words) still goes through without it. Each test fails on the code before the fix."""
import re

from tests.test_fms import sign_in, site


def forms(page):
    """Each item's assign/note form on Today, as the browser sees it."""
    return [chunk.split("</form>", 1)[0] for chunk in page.split('<form method="post" action="/work/step" class="fms-decide">')[1:]
            if 'name="text"' in chunk.split("</form>", 1)[0]]


def test_the_note_box_is_required_and_assign_does_not_need_it(fms_url, sim):
    with sign_in(site(fms_url)) as c:
        found = forms(c.get("/today").text)
        assert found
        for form in found:
            box = re.search(r'<input[^>]*name="text"[^>]*>', form).group(0)
            assert " required" in box, box                                            # an empty press is stopped on the page
            assign = re.search(r'<button[^>]*value="assign"[^>]*>', form).group(0)
            assert "formnovalidate" in assign                                         # choosing who it goes to needs no words
            for value in ("note", "finish"):
                button = re.search(rf'<button[^>]*value="{value}"[^>]*>', form)
                assert button is None or "formnovalidate" not in button.group(0)      # the note and done buttons keep the check

