import os
import tempfile
import unittest
from pathlib import Path

from sanlight_mesh.state import (
    StateError,
    read_state,
    token_from_state,
    validate_state_identity,
    write_state,
)


class StateTest(unittest.TestCase):
    def test_atomic_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "state.json"
            write_state(path, {"role": "test", "token": "0123456789abcdef"})
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(token_from_state(read_state(path), "test"), 0x0123456789ABCDEF)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits are not available")
    def test_overbroad_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"token":"1"}')
            os.chmod(path, 0o644)
            with self.assertRaises(StateError):
                read_state(path)

    def test_identity_mismatch_does_not_echo_values(self):
        with self.assertRaises(StateError) as context:
            validate_state_identity(
                {"meshUUID": "secret-old"}, {"meshUUID": "secret-new"}, "test"
            )
        message = str(context.exception)
        self.assertNotIn("secret-old", message)
        self.assertNotIn("secret-new", message)


if __name__ == "__main__":
    unittest.main()
