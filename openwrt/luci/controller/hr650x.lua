module("luci.controller.hr650x", package.seeall)

function index()
    entry({"admin", "services", "hr650x"}, call("hr650x_page"), _("HR650X 风扇"), 90).dependent = false
end

function hr650x_page()
    luci.template.render("hr650x/index")
end
