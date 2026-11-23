// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.app;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;

/**
 * Settings of the live loop, for the remote: template server URL, this TV's id and token,
 * detector mode, toasts and whether camera pictures may be served on the local network. "Test connection" tries the values as entered before saving.
 * The token is never shown; an empty token field keeps the stored one. The same settings can
 * also be changed over HTTP (/config), e.g. to paste a long token from a computer.
 */
public class SettingsActivity extends Activity {
    private Config cfg;
    private EditText server, tv, token;
    private RadioGroup mode;
    private CheckBox toasts, lanFrames;
    private TextView result;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        cfg = new Config(this);
        int pad = dp(32);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(pad * 3, pad, pad * 3, pad);
        col.setBackgroundColor(0xff181818);

        col.addView(text("adshut · settings", 30, Color.WHITE));
        col.addView(label("Template server URL"));
        server = edit(cfg.server(), "e.g. http://192.168.1.10:8766", InputType.TYPE_TEXT_VARIATION_URI);
        col.addView(server);
        col.addView(label("This TV's id at the server"));
        tv = edit(cfg.tv(), "e.g. living-room", InputType.TYPE_CLASS_TEXT);
        col.addView(tv);
        col.addView(label("Token (" + (cfg.token().length() > 0 ? "set; leave empty to keep it" : "not set") + ")"));
        token = edit("", "paste or type the token from server.py --add-tv",
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        col.addView(token);

        col.addView(label("Detector mode"));
        mode = new RadioGroup(this);
        mode.setOrientation(LinearLayout.HORIZONTAL);
        String[][] modes = {{"off", "Off (record only)"}, {"shadow", "Shadow (log, no muting)"}, {"active", "Active (mutes)"}};
        for (int i = 0; i < modes.length; i++) {
            RadioButton b = new RadioButton(this);
            b.setId(100 + i);
            b.setTag(modes[i][0]);
            b.setText(modes[i][1]);
            b.setTextColor(Color.WHITE);
            b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18);
            b.setPadding(0, 0, dp(24), 0);
            mode.addView(b);
            if (modes[i][0].equals(cfg.mode())) {
                mode.check(b.getId());
            }
        }
        col.addView(mode);
        toasts = new CheckBox(this);
        toasts.setText("Show a toast for each mute and unmute");
        toasts.setTextColor(Color.WHITE);
        toasts.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18);
        toasts.setChecked(cfg.toasts());
        col.addView(toasts);
        lanFrames = new CheckBox(this);
        lanFrames.setText("Share the camera picture on the local network (only for a host computer; off keeps every picture on this TV)");
        lanFrames.setTextColor(Color.WHITE);
        lanFrames.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18);
        lanFrames.setChecked(cfg.lanFrames());
        col.addView(lanFrames);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setPadding(0, dp(16), 0, 0);
        Button test = button("Test connection");
        Button save = button("Save");
        Button back = button("Back");
        buttons.addView(test);
        buttons.addView(save);
        buttons.addView(back);
        col.addView(buttons);
        result = text("", 18, 0xffffd54f);
        result.setPadding(0, dp(12), 0, 0);
        col.addView(result);

        test.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                testConnection();
            }
        });
        save.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                save();
            }
        });
        back.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                finish();
            }
        });

        ScrollView sv = new ScrollView(this);
        sv.setBackgroundColor(0xff181818);
        sv.addView(col);
        setContentView(sv);
        server.requestFocus();
    }

    private String serverValue() {
        return server.getText().toString().trim().replaceAll("/+$", "");
    }

    private String tokenValue() {
        String t = token.getText().toString().trim();
        return t.length() > 0 ? t : cfg.token();
    }

    private String modeValue() {
        View b = mode.findViewById(mode.getCheckedRadioButtonId());
        return b == null ? cfg.mode() : (String) b.getTag();
    }

    private void save() {
        String err = check(serverValue(), tv.getText().toString().trim());
        if (err != null) {
            result.setText(err);
            return;
        }
        String[][] kv = {{"server", serverValue()}, {"tv", tv.getText().toString().trim()},
                {"mode", modeValue()}, {"toasts", toasts.isChecked() ? "1" : "0"}};
        for (String[] p : kv) {
            String e = cfg.set(p[0], p[1]);
            if (e != null) {
                result.setText(e);
                return;
            }
        }
        cfg.setLanFrames(lanFrames.isChecked());
        if (token.getText().toString().trim().length() > 0) {
            cfg.set("token", token.getText().toString().trim());
            token.setText("");
        }
        CamService s = CamService.instance();
        if (s != null) {
            s.settingsChanged();
        }
        result.setText("Saved.");
    }

    static String check(String url, String id) {
        if (url.length() > 0 && !url.matches("^https?://[^\\s/]+(:\\d+)?(/.*)?$")) {
            return "The server URL must look like http://host:port";
        }
        if (id.length() > 0 && !id.matches("^[a-z0-9][a-z0-9-]{0,40}$")) {
            return "TV id: lowercase letters, digits and dashes";
        }
        return null;
    }

    private void testConnection() {
        final String url = serverValue(), id = tv.getText().toString().trim(), tok = tokenValue();
        String err = check(url, id);
        if (err == null && (url.length() == 0 || id.length() == 0 || tok.length() == 0)) {
            err = "Server URL, TV id and token are all needed for a test";
        }
        if (err != null) {
            result.setText(err);
            return;
        }
        result.setText("Testing…");
        new Thread(new Runnable() {
            @Override
            public void run() {
                final String msg = probe(url, id, tok);
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        result.setText(msg);
                    }
                });
            }
        }, "adshut-test").start();
    }

    /** Plain-language result of asking the server for the template set and this TV's status. */
    static String probe(String url, String id, String tok) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(url + "/v1/templates").openConnection();
            c.setConnectTimeout(5000);
            c.setReadTimeout(10000);
            c.setRequestProperty("Authorization", "Bearer " + tok);
            int code = c.getResponseCode();
            if (code == 401) {
                return "The server does not know this token.";
            }
            if (code != 200) {
                return "The server answered HTTP " + code + ".";
            }
            JSONObject idx = new JSONObject(new String(Sync.readAll(c.getInputStream()), "UTF-8"));
            int n = idx.getJSONArray("templates").length();
            c = (HttpURLConnection) new URL(url + "/v1/sync/" + URLEncoder.encode(id, "UTF-8")).openConnection();
            c.setConnectTimeout(5000);
            c.setRequestProperty("Authorization", "Bearer " + tok);
            code = c.getResponseCode();
            if (code == 403) {
                return "Connected, but the token belongs to another TV id.";
            }
            if (code != 200) {
                return "Connected, but the TV status answered HTTP " + code + ".";
            }
            return "Connected: " + n + " templates on the server, uploads allowed for " + id + ".";
        } catch (java.net.UnknownHostException e) {
            return "Unknown host: check the server URL.";
        } catch (java.net.SocketTimeoutException e) {
            return "No answer from the server (timeout).";
        } catch (java.net.ConnectException e) {
            return "Connection refused: is the server running on that port?";
        } catch (Exception e) {
            return "Failed: " + e.getMessage();
        }
    }

    // ------------------------------------------------------------ widgets

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density);
    }

    private TextView text(String s, int sp, int color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(color);
        return t;
    }

    private TextView label(String s) {
        TextView t = text(s, 18, 0xffb0b0b0);
        t.setPadding(0, dp(16), 0, dp(4));
        return t;
    }

    private EditText edit(String value, String hint, int type) {
        EditText e = new EditText(this);
        e.setText(value);
        e.setHint(hint);
        e.setInputType(type);
        e.setSingleLine(true);
        e.setTextColor(Color.WHITE);
        e.setHintTextColor(0xff777777);
        e.setTextSize(TypedValue.COMPLEX_UNIT_SP, 20);
        return e;
    }

    private Button button(String s) {
        Button b = new Button(this);
        b.setText(s);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18);
        b.setGravity(Gravity.CENTER);
        return b;
    }
}
